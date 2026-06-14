"""Path-map codegen — a declarative {field: {path, transform}} strategy.

The sibling :mod:`crawloop.loop.jsonpath` discovers fields whose oracle value
appears VERBATIM in the page's embedded JSON. That misses every NORMALIZED field:
a number the JSON stores in units of ten thousand (``12``) whose record value is
``120000``, an enum code (``"Full-time"`` -> ``"full_time"``) the JSON never spells
out, or a derived string (``location`` = city + region concatenated). Value->path
matching cannot find those, because the transformed value is not in the JSON.

So instead the LLM emits ONCE, during bootstrap, a declarative MAP describing where
each field lives AND how to transform it — ``{field: {"path": [...], "transform":
<T>}}`` — and this module's DETERMINISTIC core executes that map for free at run
time. The model is excellent at READING JSON and NAMING paths/transforms (far more
reliable than writing navigation code), and the runtime is plain, total, offline
Python. Only :func:`propose_fieldmap` touches a model; everything else here is pure.

Transform vocabulary (a field spec is ``{"path": [...], "transform": <T>}``):

* ``"none"`` — the value at ``path``, unchanged.
* ``"x10000"`` — ``int(round(float(value) * 10000))`` (units of ten thousand ->
  the full figure).
* ``"int"`` / ``"float"`` — numeric coercion of the value at ``path``.
* ``{"map": {src: code, ...}}`` — read the value, look it up SUBSTRING-tolerantly
  (if the JSON value CONTAINS a key, that key's code wins); unmapped -> None.
* ``{"concat": [[p1...], [p2...], ...], "sep": "<s>"}`` — read each sub-path from the
  ROOT, drop the empty ones, join the rest with ``sep``.
* ``{"list": [[path-to-array]], "field": "<key>" | null}`` — the value at the
  sub-path is an array; with ``field`` map each element to ``element[field]``, with
  ``null`` use the array of scalars itself.

Public surface:

* :func:`apply_field_spec` / :func:`apply_fieldmap` — the pure interpreter (the
  reference the emitted crawler mirrors). Total: any miss/error yields None.
* :func:`emit_crawler` — render a complete, AST-gate-clean crawler module that
  inlines the fieldmap + a self-contained ``_apply`` and reads the FIRST JSON island.
* :func:`propose_fieldmap` — the single LLM step: ask a model for the map, parse it,
  and VALIDATE it reproduces most of the oracle on the sample's own JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from crawloop.htmlutil import trim_html
from crawloop.llm import Completer
from crawloop.loop.jsonpath import (  # reuse the verbatim-matcher primitives
    JSON_MARKERS,
    extract_json_blobs,
    iter_leaves,  # noqa: F401 — re-exported for callers that build maps from leaves
    read_path,
)
from crawloop.schemas import schema_json

__all__ = [
    "JSON_MARKERS",
    "FieldmapProposalError",
    "apply_field_spec",
    "apply_fieldmap",
    "emit_crawler",
    "extract_json_blobs",
    "iter_leaves",
    "propose_fieldmap",
    "read_path",
]

# Fraction of the oracle's POPULATED fields a proposed map must reproduce on the
# sample's own JSON to be accepted. Below this the map is too weak to trust, so
# propose_fieldmap raises and the caller falls back to other candidates.
_MIN_REPRODUCTION = 0.60


# --------------------------------------------------------------------------- #
# The pure interpreter — the reference semantics the emitted crawler mirrors.
# --------------------------------------------------------------------------- #


def _to_number(value: Any) -> float | None:
    """Best-effort float reading of a JSON leaf, or None if it is not numeric.

    Accepts real numbers and numeric strings (stripping commas + whitespace, since
    ``__NEXT_DATA__`` occasionally stores ``"1,200"``). bools are rejected — a flag
    is not a quantity. Shared by the ``x10000`` / ``int`` / ``float`` transforms.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _apply_map(value: Any, mapping: dict) -> Any:
    """Substring-tolerant lookup: the first key the string ``value`` CONTAINS wins.

    Mirrors the enum mapping the JSON needs — the JSON stores a label
    (``"Permanent Full-time"``) that contains the source term (``"Full-time"``) whose
    code (``"full_time"``) we want. An exact match is just the len-equal case of
    containment. Unmapped (or a non-string value) -> None. Iteration order follows
    the mapping's insertion order, so a more specific key should be listed before a
    less specific one.
    """
    if not isinstance(value, str):
        return None
    for src, code in mapping.items():
        if src in value:
            return code
    return None


def _apply_concat(json_obj: Any, parts: list, sep: str) -> Any:
    """Read each sub-path from the ROOT, drop empties, join the rest with ``sep``.

    Each part is an absolute path from ``json_obj``; a part that reads None or an
    empty string is skipped. Returns the joined string, or None if NO part produced
    a value (so an all-missing concat omits the field rather than yielding "").
    """
    pieces: list[str] = []
    for sub in parts:
        v = read_path(json_obj, sub)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            pieces.append(s)
    return sep.join(pieces) if pieces else None


def _apply_list(json_obj: Any, list_paths: list, field_key: Any) -> Any:
    """The array at the (first) sub-path, optionally projected to ``element[field]``.

    ``list_paths`` is a list holding the path to the array (so the spec shape matches
    ``concat``'s list-of-paths form); the first entry is used. With ``field_key`` set,
    each dict element is mapped to ``element[field_key]`` (non-dict / missing-key
    elements are skipped); with ``None`` the array is returned as-is. A path that is
    not an array yields None.
    """
    if not list_paths:
        return None
    arr = read_path(json_obj, list_paths[0])
    if not isinstance(arr, list):
        return None
    if field_key is None:
        return arr
    out = []
    for el in arr:
        if isinstance(el, dict) and field_key in el:
            out.append(el[field_key])
    return out


def apply_field_spec(json_obj: Any, spec: dict) -> Any:
    """Evaluate one field spec against ``json_obj``; None on any miss/error.

    Reads ``spec["path"]`` and applies ``spec["transform"]`` per the module's
    vocabulary. NEVER raises: a missing path, a non-coercible value, or a malformed
    spec all yield None (the contract that keeps the emitted crawler total). The
    ``concat`` / ``list`` transforms ignore the base ``path`` and read their own
    sub-paths from the ROOT (so the map author sets ``"path": []`` for them).
    """
    try:
        transform = spec["transform"]

        # Container transforms read their own sub-paths from the root.
        if isinstance(transform, dict):
            if "map" in transform:
                return _apply_map(read_path(json_obj, spec.get("path", [])), transform["map"])
            if "concat" in transform:
                return _apply_concat(json_obj, transform["concat"], transform.get("sep", ""))
            if "list" in transform:
                return _apply_list(json_obj, transform["list"], transform.get("field"))
            return None

        # Scalar transforms read the single base path.
        value = read_path(json_obj, spec.get("path", []))
        if value is None:
            return None
        if transform == "none":
            return value
        if transform == "x10000":
            n = _to_number(value)
            return int(round(n * 10000)) if n is not None else None
        if transform == "int":
            n = _to_number(value)
            return int(n) if n is not None else None
        if transform == "float":
            n = _to_number(value)
            return float(n) if n is not None else None
        return None
    except Exception:
        # Total by construction: a malformed spec/value contributes nothing.
        return None


def apply_fieldmap(json_obj: Any, fieldmap: dict) -> dict:
    """Evaluate every spec in ``fieldmap`` and return the record, dropping Nones.

    ``{field: apply_field_spec(json_obj, spec)}`` with the None-valued fields
    removed, so a field whose path misses (or whose value cannot be transformed) is
    simply absent from the record rather than present as None.
    """
    record: dict = {}
    for field, spec in fieldmap.items():
        value = apply_field_spec(json_obj, spec)
        if value is not None:
            record[field] = value
    return record


# --------------------------------------------------------------------------- #
# emit_crawler — a complete, AST-gate-clean, self-contained crawler module.
#
# The emitted module imports only re/json + crawloop.contract, inlines the
# fieldmap as a literal dict and an `_apply` that MIRRORS apply_field_spec, extracts
# the FIRST marked JSON island, and returns one item. It contains NO dunder names,
# no banned calls, and no str.format (it uses only literals + indexing), so
# crawloop.safety.ast_check passes it and it runs under run_in_sandbox.
# --------------------------------------------------------------------------- #

# The generated module is CODE (a crawler), not prose, so it lives here as a format
# template rather than in prompts/. The generator fills it with str.format on the
# PYTHON side; the EMITTED source itself uses no .format (the AST gate bans it). The
# `_apply` body is fixed scaffolding — only the marker regex, class identity,
# schema_ref and the inlined fieldmap literal are interpolated.
_MODULE_TEMPLATE = '''\
"""Auto-generated path-map crawler for {family!r} (deterministic; map by LLM, run free).

Generated by crawloop.loop.pathmap.emit_crawler: each field is read from a
declarative {{path, transform}} spec the model proposed once, executed here by a
self-contained interpreter — no navigation logic is guessed at run time.
"""

import re
import json

from crawloop.contract import Crawler, CrawlResult

_BLOB_RE = re.compile(
    r"<script\\b[^>]*(?:{markers})[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)

# The declarative field map: {{field: {{"path": [...], "transform": <T>}}}}.
_FIELDMAP = {fieldmap_literal}


def _read(obj, path):
    """Follow ``path`` (dict keys / list indices) into ``obj``; None on any miss."""
    cur = obj
    for step in path:
        if isinstance(cur, dict):
            if step not in cur:
                return None
            cur = cur[step]
        elif isinstance(cur, list):
            if not isinstance(step, int) or isinstance(step, bool):
                return None
            if step < 0 or step >= len(cur):
                return None
            cur = cur[step]
        else:
            return None
    return cur


def _number(value):
    """Best-effort float of a JSON leaf (parsing comma-grouped strings); None if not."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _apply(data, spec):
    """Evaluate ONE field spec against the root ``data``; None on any miss/error.

    Mirrors crawloop.loop.pathmap.apply_field_spec so the emitted crawler is
    self-contained and total — any malformed spec or value yields None, never raises.
    """
    try:
        transform = spec["transform"]
        if isinstance(transform, dict):
            if "map" in transform:
                value = _read(data, spec.get("path", []))
                if not isinstance(value, str):
                    return None
                for src, code in transform["map"].items():
                    if src in value:
                        return code
                return None
            if "concat" in transform:
                sep = transform.get("sep", "")
                pieces = []
                for sub in transform["concat"]:
                    v = _read(data, sub)
                    if v is None:
                        continue
                    s = str(v).strip()
                    if s:
                        pieces.append(s)
                return sep.join(pieces) if pieces else None
            if "list" in transform:
                paths = transform["list"]
                if not paths:
                    return None
                arr = _read(data, paths[0])
                if not isinstance(arr, list):
                    return None
                field_key = transform.get("field")
                if field_key is None:
                    return arr
                out = []
                for el in arr:
                    if isinstance(el, dict) and field_key in el:
                        out.append(el[field_key])
                return out
            return None

        value = _read(data, spec.get("path", []))
        if value is None:
            return None
        if transform == "none":
            return value
        if transform == "x10000":
            n = _number(value)
            return int(round(n * 10000)) if n is not None else None
        if transform == "int":
            n = _number(value)
            return int(n) if n is not None else None
        if transform == "float":
            n = _number(value)
            return float(n) if n is not None else None
        return None
    except Exception:
        return None


class {classname}(Crawler):
    family = {family!r}
    schema_ref = {schema_ref!r}

    async def crawl(self, url, ctx):
        html = await ctx.fetch(url)
        match = _BLOB_RE.search(html)
        if match is None:
            return CrawlResult(items=[])
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            return CrawlResult(items=[])

        item = {{}}
        for field, spec in _FIELDMAP.items():
            value = _apply(data, spec)
            if value is not None:
                item[field] = value
        return CrawlResult(items=[item])
'''


def _classname(family: str) -> str:
    """A valid, dunder-free Python class name derived from ``family``.

    Keeps only alphanumerics from the family id, title-cases word boundaries, and
    suffixes ``PathMapCrawler``. A leading digit (or an empty result) is prefixed so
    the identifier is always valid — and it never contains ``__`` so it cannot trip
    the AST gate's dunder rule.
    """
    parts = re.split(r"[^0-9A-Za-z]+", family)
    stem = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not stem or stem[0].isdigit():
        stem = "Family" + stem
    return stem + "PathMapCrawler"


def emit_crawler(
    *,
    family: str,
    schema_ref: str,
    json_markers: list[str],
    fieldmap: dict,
) -> str:
    """Render a complete crawler module source string for ``fieldmap``.

    The module imports only ``re`` / ``json`` and ``crawloop.contract`` (so it
    passes :func:`crawloop.safety.ast_check` — no dunder, no banned calls, no
    ``str.format``), defines one :class:`~crawloop.contract.Crawler` subclass with
    the given ``family`` / ``schema_ref``, extracts the FIRST ``<script>`` JSON island
    whose open tag matches any of ``json_markers``, and executes ``fieldmap`` with a
    self-contained ``_apply`` that mirrors :func:`apply_field_spec`. A field whose
    spec misses on a page is omitted from the item; no island ->
    ``CrawlResult(items=[])``.

    ``fieldmap`` is inlined as a Python literal via ``repr`` — the spec values are
    plain dicts/lists/str/int/None, which ``repr`` renders as valid, side-effect-free
    literals (no names, no calls), keeping the emitted source within the gate. The
    marker alternation is regex-escaped so a metacharacter-bearing marker
    (``application/ld+json`` has a ``+``) matches literally.
    """
    markers = "|".join(re.escape(m) for m in json_markers)
    return _MODULE_TEMPLATE.format(
        family=family,
        schema_ref=schema_ref,
        classname=_classname(family),
        markers=markers,
        fieldmap_literal=repr(fieldmap),
    )


# --------------------------------------------------------------------------- #
# propose_fieldmap — the single LLM step (map proposal + validation).
# --------------------------------------------------------------------------- #

# Prompts live in the top-level prompts/ dir (sibling of the package), the same
# convention codegen.py / fallback.py use. Loaded ONCE at import: they are static.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
SYSTEM = (_PROMPTS_DIR / "pathmap_system.txt").read_text(encoding="utf-8")
USER_TEMPLATE = Template((_PROMPTS_DIR / "pathmap_user.txt").read_text(encoding="utf-8"))

# Strips an opening ```/```json fence (and the closing ```) some models wrap JSON
# in despite being told not to. Tolerant of surrounding whitespace.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class FieldmapProposalError(Exception):
    """Raised by :func:`propose_fieldmap` when the model's map could not be parsed
    as a JSON object or did not reproduce enough of the oracle to be trusted.

    The caller (the Loop driver) treats this as "the path-map strategy did not
    apply" and falls back to the other candidates — it is never fatal."""


def _parse_fieldmap(raw: str) -> dict:
    """Parse a model completion into a fieldmap dict, or raise FieldmapProposalError.

    Strips an optional ```json fence, ``json.loads`` the rest, and enforces that the
    top level is a JSON OBJECT of ``{field: {"path": ..., "transform": ...}}`` shaped
    specs. A non-JSON body, a non-object top level, or a value that is not a spec dict
    raises with a clear reason.
    """
    text = _FENCE_RE.sub("", raw).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FieldmapProposalError(f"map was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FieldmapProposalError(
            f"expected a JSON object mapping fields to specs, got {type(parsed).__name__}"
        )
    for field, spec in parsed.items():
        if not isinstance(spec, dict) or "transform" not in spec:
            raise FieldmapProposalError(
                f"field {field!r} maps to {spec!r}, not a {{path, transform}} spec"
            )
    return parsed


def _values_agree(got: Any, want: Any) -> bool:
    """Whether a produced value matches the oracle's, tolerant of number formatting.

    Two values agree if both read as numbers and are numerically equal, or otherwise
    if their ``str``-stripped forms are equal. This keeps a price emitted as ``int``
    matching an oracle ``int``/``float`` and a list matching a list, without a brittle
    exact-type check.
    """
    gn, wn = _to_number(got), _to_number(want)
    if gn is not None and wn is not None:
        return gn == wn
    return str(got).strip() == str(want).strip()


def _reproduction_ratio(blob: Any, fieldmap: dict, oracle_record: dict) -> float:
    """Fraction of the oracle's POPULATED fields the map reproduces from ``blob``.

    Applies ``fieldmap`` to the sample's own JSON and counts how many of the oracle's
    non-null fields it reproduces with a matching value (numbers compared as floats so
    ``120000`` vs ``120000.0`` agrees; everything else by ``str``-equality after a
    strip). The denominator is the count of the oracle's populated fields; 1.0 when the
    oracle has none (nothing to reproduce — vacuously fine).
    """
    populated = {k: v for k, v in oracle_record.items() if v is not None}
    if not populated:
        return 1.0
    produced = apply_fieldmap(blob, fieldmap)
    hits = sum(
        1
        for field, want in populated.items()
        if field in produced and _values_agree(produced[field], want)
    )
    return hits / len(populated)


async def propose_fieldmap(
    sample_html: str,
    oracle_record: dict,
    schema_ref: str,
    completer: Completer,
    *,
    model: str = "anthropic/claude-fable-5",
) -> dict:
    """Ask a model for the field map, parse it, and VALIDATE it on the sample.

    Builds the prompt from the target schema, the trusted ``oracle_record``, and the
    sample's embedded JSON (the FIRST island, bounded via
    :func:`~crawloop.htmlutil.trim_html`), makes ONE completer call, parses the
    JSON map (stripping a code fence if present), and checks the map reproduces at
    least :data:`_MIN_REPRODUCTION` of the oracle's populated fields when applied to
    the sample's own JSON. Returns the validated fieldmap.

    Raises :class:`FieldmapProposalError` when the sample has no JSON island, the
    completion does not parse as a ``{field: spec}`` object, or the map reproduces too
    little of the oracle — so the Loop driver can fall back to the other candidates.
    """
    blobs = extract_json_blobs(sample_html)
    if not blobs:
        raise FieldmapProposalError("sample has no embedded JSON island to map")
    blob = blobs[0]

    user = USER_TEMPLATE.safe_substitute(
        schema_json=json.dumps(schema_json(schema_ref), indent=2),
        oracle=json.dumps(oracle_record, ensure_ascii=False, indent=2, default=str),
        blob=trim_html(sample_html),
    )
    raw = await completer.complete(system=SYSTEM, user=user, model=model)
    fieldmap = _parse_fieldmap(raw)

    ratio = _reproduction_ratio(blob, fieldmap, oracle_record)
    if ratio < _MIN_REPRODUCTION:
        raise FieldmapProposalError(
            f"proposed map reproduced only {ratio:.0%} of the oracle's populated "
            f"fields (need >= {_MIN_REPRODUCTION:.0%})"
        )
    return fieldmap
