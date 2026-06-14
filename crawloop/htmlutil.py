"""Cheap HTML reduction for LLM prompts.

The T2 extractor (:mod:`crawloop.fallback`) sends page HTML to a model, and
raw HTML is mostly tokens the model does not need: inline scripts, CSS, SVG path
data, and comments carry no extractable records but cost money on every call.
:func:`trim_html` is a deliberately cheap, dependency-free pass that strips those
out, collapses whitespace, and caps total length — while preserving the tag
structure and the attributes a model actually uses to locate data
(``class`` / ``id`` / ``data-*`` / ``aria-*``). It is pure (no I/O) so it is
trivially testable and reusable by anything else that needs a smaller HTML
string.

This is intentionally NOT a real HTML parser: a regex pass is enough for prompt
cost reduction and avoids pulling in a parser dependency. It never executes or
interprets the HTML; worst case it leaves a little extra markup in, which is
harmless for the prompt.
"""

from __future__ import annotations

import os
import re

# ``<style>`` / ``<svg>`` contents are pure noise for extraction: drop the whole
# element, open tag through close tag. DOTALL so multi-line bodies are caught;
# IGNORECASE so <SVG>/<Style> match too.
_DROP_STYLE_SVG_RE = re.compile(
    r"<(style|svg)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# Capture the BODY of a structured-data script — schema.org JSON-LD, a Next.js
# ``__NEXT_DATA__`` payload, or any ``application/json`` island. On server-rendered
# sites these hold the COMPLETE record as clean JSON and are by far the best
# extraction source. The lookahead scans only the open tag (``[^>]*`` stops at
# ``>``); group 1 is the script's inner JSON.
_DATA_SCRIPT_RE = re.compile(
    r"<script\b[^>]*(?:application/(?:ld\+)?json|__NEXT_DATA__)[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)

# Any ``<script>`` element — used to strip ALL scripts from the HTML body after the
# data ones have been captured and hoisted.
_ANY_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)

# HTML comments, including conditional/multi-line ones.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Any run of whitespace (incl. newlines/tabs) -> a single space. This shrinks the
# pretty-printing between tags without disturbing single-spaced tag attributes.
_WS_RE = re.compile(r"\s+")

# Default cap, overridable per-process via env so large real-world pages aren't
# starved of context (set it for big pages; unit tests leave it unset → 8000).
_DEFAULT_CAP_ENV = "CRAWLER_LOOP_HTML_CAP"


def trim_html(html: str, max_chars: int | None = None) -> str:
    """Reduce ``html`` to a cheaper-to-tokenize string, capped at ``max_chars``.

    Steps:

    1. **Hoist structured-data JSON to the front.** Any ``application/ld+json`` /
       ``application/json`` / ``__NEXT_DATA__`` script body is extracted and placed
       FIRST. On a server-rendered page this blob is often the full record as clean
       JSON — but it can sit late in a large document (e.g. deep into a 350k-byte
       page), so without hoisting the length cap would chop it off entirely and the
       model would never see the gold. Hoisting guarantees both the oracle and
       codegen see the complete record first.
    2. Strip ``<style>`` / ``<svg>`` / all ``<script>`` and comments from the HTML
       body, collapse whitespace. The de-scripted body is appended after the JSON
       as a fallback for fields not in the blob.
    3. Truncate the whole thing to ``max_chars`` (default ``CRAWLER_LOOP_HTML_CAP``
       env, else 8000) — JSON-first, so the cap trims trailing HTML, not the data.

    Note the runtime crawler always fetches the FULL page, so even if the prompt's
    JSON view is truncated, a generated ``json.loads`` crawler parses the complete
    blob at run time; the cap only bounds what the model READS.
    """
    if max_chars is None:
        max_chars = int(os.getenv(_DEFAULT_CAP_ENV, "8000"))
    blobs = [m.group(1).strip() for m in _DATA_SCRIPT_RE.finditer(html) if m.group(1).strip()]
    body = _DROP_STYLE_SVG_RE.sub("", html)
    body = _ANY_SCRIPT_RE.sub("", body)
    body = _COMMENT_RE.sub("", body)
    body = _WS_RE.sub(" ", body).strip()
    if blobs:
        prefix = _WS_RE.sub(" ", "[[STRUCTURED-DATA-JSON]] " + " ".join(blobs)).strip()
        out = f"{prefix} [[HTML]] {body}"
    else:
        out = body
    return out[:max_chars]
