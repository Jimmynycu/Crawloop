"""T2 direct extraction — the LLM fallback, and the Loop's oracle.

When the generated crawlers (the cheap, fast T1 path) all fail, the engine drops
to T2: hand the page HTML and the target JSON Schema to a model and let it
extract the records directly. :func:`direct_extract` is that fallback. It is also
the Loop's **oracle**: in M9 the regeneration gauntlet calls this exact function
per sample page to get a trusted "what the answer should be", which a candidate
crawler's output is then scored against (validator Gate 3). One function, two
callers — there is a single source of truth for "what an LLM extracts from this
page", so the fallback and the oracle can never drift apart.

The flow is build-prompt -> complete -> parse -> validate, with a bounded repair
loop: if the model returns unparseable text or output that fails validation, the
error is fed back into the prompt and the model is asked to correct itself, up to
``max_repairs`` times. Still bad after that -> :class:`ExtractionFailed`.

Prompt text lives in ``prompts/extract_system.txt`` and
``prompts/extract_user.txt`` (house rule: long-form prose is not inlined in
code). The user prompt is a :class:`string.Template` so the JSON Schema's braces
pass through untouched ($-placeholders, not ``str.format``).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from string import Template

from crawloop.htmlutil import trim_html
from crawloop.llm import Completer
from crawloop.schemas import schema_json
from crawloop.validator import validate

# Prompt files live in the top-level prompts/ dir (sibling of the package), the
# same "repo root = parent of the package directory" convention schemas.py uses.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Loaded ONCE at import: the prompts are static, so there is no reason to hit the
# filesystem on every extraction. SYSTEM is sent verbatim; USER_TEMPLATE is
# filled per call via safe_substitute (so a stray $ or the schema's braces can
# never raise a KeyError/ValueError mid-extraction).
SYSTEM = (_PROMPTS_DIR / "extract_system.txt").read_text(encoding="utf-8")
USER_TEMPLATE = Template((_PROMPTS_DIR / "extract_user.txt").read_text(encoding="utf-8"))

# Strips an opening ```/```json fence (and the closing ```) some models wrap JSON
# in despite being told not to. Tolerant of surrounding whitespace.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# The oracle / T2 path hands the model the hoisted JSON island with a much larger
# budget than the default 8000 HTML cap, so a wide 100K+ ``__NEXT_DATA__`` /
# ``ld+json`` island is read in full (otherwise the gold record is chopped off and
# the oracle returns empty, starving the regeneration loop). Overridable per-process.
_ORACLE_JSON_CAP_ENV = "CRAWLER_LOOP_ORACLE_JSON_CAP"
_DEFAULT_ORACLE_JSON_CAP = 120_000
# ...and the oracle/T2 also needs the de-scripted HTML body in FULL, not the tiny
# 8000-char default: a real multi-record LISTING page has no JSON island, so the
# whole page must reach the model or it only sees the first few records (the cap
# truncated real 20-item pages to ~3, giving the loop wrong ground truth).
_ORACLE_HTML_CAP_ENV = "CRAWLER_LOOP_ORACLE_HTML_CAP"
_DEFAULT_ORACLE_HTML_CAP = 200_000


class ExtractionFailed(Exception):
    """Raised by :func:`direct_extract` when the model could not produce valid,
    schema-passing JSON even after the repair budget was spent. ``reason`` is the
    last parse/validation error, so the engine and logs can see *why* T2 failed
    (not just that it did)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _parse_items(raw: str) -> list[dict]:
    """Parse a model response into a list-of-dicts, or raise ``ValueError``.

    Strips an optional ```json``` code fence, ``json.loads`` the rest, and
    enforces the structural contract the schema validator assumes: the top level
    must be a JSON array and every element an object. A clear ``ValueError`` here
    becomes the error text fed back into the repair prompt.
    """
    text = _FENCE_RE.sub("", raw).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"output was not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"expected a JSON array of objects, got {type(parsed).__name__}")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("every element of the JSON array must be an object")
    return parsed


async def direct_extract(
    html: str,
    schema_ref: str,
    completer: Completer,
    *,
    model: str = "anthropic/claude-fable-5",
    max_repairs: int = 1,
    json_max_chars: int | None = None,
    source_url: str | None = None,
) -> list[dict]:
    """Extract schema-valid records from ``html`` using ``completer`` (T2 / oracle).

    Builds the user prompt from the (trimmed) HTML and the target schema's JSON,
    asks the model for a JSON array, parses it, and validates it with
    :func:`crawloop.validator.validate`. On a parse error OR a failing
    validation report it REPAIRS: it re-prompts the model with the error appended
    (``"Your previous output was invalid: {error}. Return corrected JSON only."``)
    and re-parses + re-validates, up to ``max_repairs`` times (so at most
    ``1 + max_repairs`` model calls). The first attempt whose output parses and
    validates wins and its items are returned; if none do, raises
    :class:`ExtractionFailed` carrying the final error.

    This is both the production T2 fallback and the M9 Loop oracle — see the
    module docstring. Keeping it as one function means the "ground truth" the
    gauntlet scores candidates against is produced by the very same code path
    that serves real extractions.
    """
    if json_max_chars is None:
        json_max_chars = int(os.getenv(_ORACLE_JSON_CAP_ENV, str(_DEFAULT_ORACLE_JSON_CAP)))
    html_max_chars = int(os.getenv(_ORACLE_HTML_CAP_ENV, str(_DEFAULT_ORACLE_HTML_CAP)))
    base_user = USER_TEMPLATE.safe_substitute(
        schema_json=json.dumps(schema_json(schema_ref)),
        html=trim_html(html, max_chars=html_max_chars, json_max_chars=json_max_chars),
        source_url=source_url or "",
    )

    last_error = ""
    # 1 initial attempt + max_repairs corrective attempts.
    for attempt in range(max_repairs + 1):
        user = base_user if attempt == 0 else (
            f"{base_user}\n\nYour previous output was invalid: {last_error}. "
            "Return corrected JSON only."
        )
        raw = await completer.complete(system=SYSTEM, user=user, model=model)

        try:
            items = _parse_items(raw)
        except ValueError as exc:
            last_error = str(exc)
            continue

        report = validate(items, schema_ref)
        if report.ok:
            return items
        last_error = report.reason

    raise ExtractionFailed(last_error or "extraction produced no valid output")
