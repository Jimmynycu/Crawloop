"""The Loop's CODEGEN step (Task 9.2): turn samples + oracle into candidate source.

Given the sample pages from the sampler (Task 9.1) and the oracle's trusted
extraction of each (the T2 :func:`~crawloop.fallback.direct_extract` output),
this asks a model to WRITE a crawler module for the family and returns the
gate-passing candidate sources. The downstream gauntlet (M9b) then runs each in
the sandbox (Task 9.3) and scores it; this module neither runs nor scores — it
only produces source that is *safe to run*.

Flow: build ONE prompt (system + a ``string.Template`` user prompt carrying the
schema JSON, a static contract brief, the labelled trimmed samples, the per-sample
oracle JSON, the previous version, and any failure report) -> call the completer
``k`` times for ``k`` independent candidates -> pull the ```python``` block out of
each completion -> run every candidate through :func:`~crawloop.safety.ast_check`
and KEEP ONLY those with zero violations. A completion with no code fence, or one
whose code trips the gate, contributes no candidate (it is dropped, never fatal),
so the list returned is exactly "the candidates we are willing to execute" — and
may be empty if the model produced nothing safe.

House rule: the prose (system prompt, user template, contract brief) lives in
``prompts/codegen_*.txt`` and is loaded here, never inlined.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from crawloop.htmlutil import trim_html
from crawloop.llm import Completer
from crawloop.safety import ast_check
from crawloop.schemas import schema_json

# Prompts live in the top-level prompts/ dir (sibling of the package), the same
# convention fallback.py / schemas.py use. Loaded ONCE at import: they are static.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
SYSTEM = (_PROMPTS_DIR / "codegen_system.txt").read_text(encoding="utf-8")
USER_TEMPLATE = Template((_PROMPTS_DIR / "codegen_user.txt").read_text(encoding="utf-8"))
CONTRACT_BRIEF = (_PROMPTS_DIR / "codegen_contract.txt").read_text(encoding="utf-8")

# Pulls the body of the FIRST fenced block out of a completion. Accepts an
# optional language tag (```python / ```py / bare ```), is case-insensitive, and
# is non-greedy so it stops at the first closing fence. Group 1 is the code.
_CODE_BLOCK_RE = re.compile(
    r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)


def _contract_brief() -> str:
    """A short, accurate description of the Crawler/CrawlResult/FetchContext API
    and the ``ctx`` helpers, embedded as ``$contract`` in the user prompt.

    Kept in ``prompts/codegen_contract.txt`` (not inlined) so the prose can be
    edited and reviewed without touching code; this is just the loader.
    """
    return CONTRACT_BRIEF


def extract_code_block(text: str) -> str:
    """Return the body of the first ```...``` fenced block in ``text``.

    Tolerates a language tag (```python``/```py``/bare ```) and surrounding
    prose. Raises :class:`ValueError` if ``text`` contains no fenced block, so a
    model reply that is pure prose ("sorry, I can't…") surfaces as a clear,
    droppable error rather than being mistaken for code.
    """
    match = _CODE_BLOCK_RE.search(text)
    if match is None:
        raise ValueError("no fenced code block found in completion")
    return match.group(1)


def _format_samples(samples: list[tuple[str, str]]) -> str:
    """Label and trim each sample page for the ``$samples`` slot.

    Each page is trimmed (:func:`~crawloop.htmlutil.trim_html`) to bound
    prompt cost and headed with its index + URL so the model can line samples up
    with their oracle entries.
    """
    blocks = []
    for i, (url, html) in enumerate(samples, start=1):
        blocks.append(f"--- SAMPLE {i}: {url} ---\n{trim_html(html)}")
    return "\n\n".join(blocks)


def _format_oracle(samples: list[tuple[str, str]], oracle_jsons: list[list[dict]]) -> str:
    """Label each sample's oracle records for the ``$oracle`` slot.

    Uses the SAME index/URL heading as :func:`_format_samples` so sample N and
    its oracle line up. ``default=str`` keeps non-JSON-native values (e.g.
    ``Decimal`` prices the oracle may carry) serialisable.
    """
    blocks = []
    for i, ((url, _html), records) in enumerate(zip(samples, oracle_jsons), start=1):
        dumped = json.dumps(records, indent=2, default=str)
        blocks.append(f"--- ORACLE {i}: {url} ---\n{dumped}")
    return "\n\n".join(blocks)


async def generate_candidates(
    samples: list[tuple[str, str]],
    oracle_jsons: list[list[dict]],
    schema_ref: str,
    prev_source: str | None,
    failure_report: str | None,
    completer: Completer,
    *,
    model: str = "anthropic/claude-fable-5",
    k: int = 2,
) -> list[str]:
    """Generate up to ``k`` gate-passing candidate crawler sources.

    Builds the prompt once from ``samples`` + ``oracle_jsons`` + the target
    schema (and the optional ``prev_source`` / ``failure_report``), then calls
    ``completer.complete`` ``k`` times — each call an independent candidate. For
    every completion it extracts the ```python``` block and runs
    :func:`~crawloop.safety.ast_check`; a candidate is KEPT only if it has a
    code block AND zero violations. Completions with no fence, or whose code
    trips the gate, are dropped (not fatal). Returns the kept sources in call
    order — possibly empty. Does NOT execute any candidate (see Task 9.3).
    """
    user = USER_TEMPLATE.safe_substitute(
        schema_json=json.dumps(schema_json(schema_ref), indent=2),
        contract=_contract_brief(),
        samples=_format_samples(samples),
        oracle=_format_oracle(samples, oracle_jsons),
        # Explicit sentinels so an absent prev/failure never leaves a dangling
        # $previous / $failure token in the rendered prompt.
        previous=prev_source if prev_source else "none",
        failure=failure_report if failure_report else "none",
    )

    kept: list[str] = []
    for _ in range(k):
        completion = await completer.complete(system=SYSTEM, user=user, model=model)
        try:
            source = extract_code_block(completion)
        except ValueError:
            continue  # no code in this completion -> no candidate
        if ast_check(source):  # non-empty list == gate violation(s)
            continue  # unsafe -> never run it; drop the candidate
        kept.append(source)
    return kept
