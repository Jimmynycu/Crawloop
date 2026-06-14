"""Deterministic value-path crawler generation — pure, offline, no LLM.

Many sites server-render the COMPLETE record as a JSON island in the page: a
``<script id="__NEXT_DATA__" type="application/json">`` blob, an ``application/ld+json``
block, or a plain ``application/json`` script. The record is all there — but it can
be 100K+ chars deep and irregularly nested, which is exactly what LLM-written
navigation code gets wrong. We don't have to guess, though: for each sample we ALSO
hold the oracle — the trusted extracted value for every field. So we can
DETERMINISTICALLY discover where each oracle value lives in the JSON (value -> path)
and emit a tiny crawler that reads those exact paths. No model, no network, no
guessing — reliable and free.

The discovery is pure functions over already-parsed JSON, so the whole path is unit
testable with no I/O:

* :func:`iter_leaves` — every scalar leaf of a JSON object with its path.
* :func:`read_path` — follow a path; None on any miss (never raises).
* :func:`extract_json_blobs` — parse the marked ``<script>`` JSON islands out of
  HTML (same markers as :mod:`crawloop.htmlutil`).
* :func:`discover_paths` — value -> path discovery across N samples, returning a
  :class:`Discovery` of cross-sample-consistent ``{field: path}`` plus the per-
  numeric-field ``scale`` (1 or 10000, the unit-scaling factor).
* :func:`emit_crawler` — render a complete, AST-gate-clean crawler module that reads
  those literal paths at run time.

Enum-typed fields are intentionally NOT discovered here: their oracle value is a
normalized code (``"full_time"``) that does not appear verbatim in the JSON (which
stores a source label like ``"Full-time"``), so value->path matching cannot find
them. The caller passes them in ``skip_fields`` and lets LLM codegen handle the
mapping; this module owns only the fields that DO appear verbatim (numbers, counts,
names, urls).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# JSON islands carry the full record on server-rendered pages. These are
# the SAME markers crawloop.htmlutil keys on, so discovery reads exactly what
# the rest of the system treats as structured data. A script is a candidate iff its
# OPEN tag contains one of these substrings.
_DATA_SCRIPT_RE = re.compile(
    r"<script\b[^>]*(?:application/(?:ld\+)?json|__NEXT_DATA__)[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)

# The marker SUBSTRINGS the emitted crawler matches in a script's open tag, kept as
# one public list so the driver and emit_crawler share a single source of truth
# (the same set _DATA_SCRIPT_RE / crawloop.htmlutil key on).
JSON_MARKERS = ["__NEXT_DATA__", "application/ld+json", "application/json"]

# The unit-scaling factor: some feeds store a quantity in units of ten thousand
# (a JSON leaf of 12 meaning 120000) while the oracle holds the full figure. We try
# the leaf as-is and *10000.
_SCALE = 10000

# Relative tolerance for numeric value-matching, so price/area *format* (a stored
# float vs an int, a trailing .0) never blocks a match — only a real value
# difference does. Mirrors the validator's effectively-exact volatile comparison.
_REL_TOL = 1e-9


def iter_leaves(obj: Any, prefix: tuple = ()) -> Iterator[tuple[tuple, Any]]:
    """Yield ``(path, leaf_value)`` for every scalar leaf reachable in ``obj``.

    Recurses ``dict`` values (path element = the key) and ``list``/``tuple``
    elements (path element = the int index); every str/int/float/bool/None
    encountered is a leaf and is yielded with the path that reaches it. Containers
    themselves are never yielded — only their scalar leaves. The path is a tuple so
    it is hashable (usable as a dict key / set member in :func:`discover_paths`).
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_leaves(value, (*prefix, key))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            yield from iter_leaves(value, (*prefix, i))
    else:
        # Scalar (incl. None) — a leaf. Any non-container JSON value lands here.
        yield (prefix, obj)


def read_path(obj: Any, path: tuple | list) -> Any:
    """Follow ``path`` (dict keys / list indices) into ``obj``; None on any miss.

    Never raises: a missing key, an out-of-range or wrong-typed index, or stepping
    into a scalar all yield ``None``. An empty path returns ``obj`` itself. This is
    the read primitive the emitted crawler inlines, so its miss-is-None contract is
    what makes the generated code total.
    """
    cur = obj
    for step in path:
        if isinstance(cur, dict):
            if step not in cur:
                return None
            cur = cur[step]
        elif isinstance(cur, (list, tuple)):
            if not isinstance(step, int) or isinstance(step, bool):
                return None
            if step < 0 or step >= len(cur):
                return None
            cur = cur[step]
        else:
            # Cannot descend into a scalar.
            return None
    return cur


def extract_json_blobs(html: str) -> list[Any]:
    """Parse every marked JSON island out of ``html`` into Python objects.

    Finds the body of each ``<script>`` whose open tag carries a JSON marker
    (``__NEXT_DATA__`` / ``application/ld+json`` / ``application/json``) and
    ``json.loads`` it; a body that does not parse is skipped (not fatal). Returns
    the parsed objects in document order — possibly empty when the page has no JSON
    island. Plain ``<script>`` tags with no marker are never considered.
    """
    blobs: list[Any] = []
    for match in _DATA_SCRIPT_RE.finditer(html):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            blobs.append(json.loads(body))
        except (json.JSONDecodeError, ValueError):
            continue  # not JSON (or truncated) -> skip this island
    return blobs


@dataclass
class Discovery:
    """The result of :func:`discover_paths`.

    ``paths`` maps each discovered field (numeric OR string) to the literal path (a
    list of dict keys / int indices) at which its value lives in the page JSON —
    consistent across every sample. ``scale`` is populated ONLY for numeric fields:
    it maps the field to the factor the JSON leaf must be multiplied by to equal the
    oracle value (``1`` when the leaf already matches, ``10000`` when the JSON stores
    the value in units of ten thousand and the oracle holds the full figure). A field
    present in ``scale`` is therefore exactly a numeric field; a discovered field
    absent from ``scale`` is a string field stored verbatim. Only fields with a
    cross-sample-consistent path appear at all.
    """

    paths: dict[str, list] = field(default_factory=dict)
    scale: dict[str, int] = field(default_factory=dict)


def _to_float(value: Any) -> float | None:
    """Best-effort float reading of a JSON leaf, or None if it is not numeric.

    Accepts real numbers and numeric strings (stripping commas and surrounding
    whitespace, since ``__NEXT_DATA__`` occasionally stores ``"3,135"``). bools are
    rejected — a flag is not a quantity. Shared by the matcher and the emitted
    crawler's coercion, so "what counts as numeric" lives in one place.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _close(a: float, b: float) -> bool:
    """Whether two floats are equal within the relative tolerance (so price/area
    *format* never blocks a value match)."""
    return abs(a - b) <= _REL_TOL * max(abs(a), abs(b), 1.0)


def _numeric_scales(leaf: Any, target: float) -> set[int]:
    """The scales in {1, 10000} for which ``leaf * scale == target`` (numerically).

    Handles the unit-scaling ambiguity: the leaf may already be in the oracle's unit
    (scale 1), or stored in units of ten thousand so it needs ``*10000`` to reach the
    full figure (scale 10000). Accepting the leaf as ``target/10000`` is the same
    scale-10000 relation viewed from the other side, so it collapses to those two
    candidate scales. Returns every scale that matches (usually one); empty if the
    leaf is non-numeric or matches at neither scale.
    """
    leaf_f = _to_float(leaf)
    if leaf_f is None:
        return set()
    scales: set[int] = set()
    if _close(leaf_f, target):
        scales.add(1)
    if _close(leaf_f * _SCALE, target):
        scales.add(_SCALE)
    return scales


def _candidate_scales(leaf: Any, target: Any, *, numeric: bool) -> set[int]:
    """Scales at which ``leaf`` matches ``target`` for one field.

    For a numeric field this is :func:`_numeric_scales` (empty if the oracle target
    itself is not numeric); for a string field it is ``{1}`` when ``str(v).strip()``
    of both sides are equal, else empty. Returning a *set of scales* (rather than a
    bool) lets :func:`discover_paths` intersect on the (path, scale) pair across
    samples, so the chosen scale generalizes too — not just the path.
    """
    if numeric:
        target_f = _to_float(target)
        return _numeric_scales(leaf, target_f) if target_f is not None else set()
    return {1} if str(leaf).strip() == str(target).strip() else set()


def _path_scales_for_value(
    leaves: list[tuple[tuple, Any]], target: Any, *, numeric: bool
) -> dict[tuple, set[int]]:
    """For one sample, ``{path: {scales}}`` over every leaf that matches ``target``.

    Walks the precomputed leaf list once; for each leaf whose value matches the
    oracle ``target`` (at any scale), records that path with the set of scales that
    made it match. A path absent from the result simply did not match this sample's
    target.
    """
    out: dict[tuple, set[int]] = {}
    for path, leaf in leaves:
        scales = _candidate_scales(leaf, target, numeric=numeric)
        if scales:
            out[path] = scales
    return out


def _path_sort_key(path: tuple) -> tuple:
    """A total-order key for a JSON path tuple (mixed str keys + int indices).

    Python cannot compare ``str`` to ``int``, so a raw tuple of mixed step types is
    unorderable. We sort by ``(len(path), str-rendered steps)``: shorter paths first
    (the shallower location is the more natural one), then lexicographically by each
    step's string form. Deterministic and stable for "lexicographically-smallest".
    """
    return (len(path), tuple(str(step) for step in path))


def discover_paths(
    sample_jsons: list,
    oracle_records: list[dict],
    *,
    numeric_fields: set[str],
    skip_fields: frozenset[str] | set[str] = frozenset(),
) -> Discovery:
    """Discover, for each field, the JSON path that holds its oracle value in EVERY
    sample.

    ``sample_jsons[i]`` is the parsed page JSON for sample ``i`` and
    ``oracle_records[i]`` is that sample's trusted record (a flat ``{field: value}``
    dict). A field is CONSIDERED only when it is non-null in every sample's oracle
    and not in ``skip_fields``.

    For each considered field:

    1. In every sample, index all ``(path, {scales})`` whose leaf value matches that
       sample's oracle value (strings by ``str(v).strip()`` equality; ``numeric_fields``
       by ``float`` value at scale 1 or 10000 — the unit-scaling factor — tolerant of
       format and of leaves stored as numeric strings).
    2. Intersect the (path, scale) pairs across all samples, so only a path that
       holds the value — at the SAME scale — in EVERY sample survives. This is what
       makes the path generalize instead of overfitting page 0.
    3. Record the lexicographically-smallest surviving path; for a numeric field
       also record its scale (string fields carry no scale entry).

    Returns a :class:`Discovery`; a field with no surviving path is absent from it.
    Enum-typed fields are expected in ``skip_fields`` (their normalized oracle code
    never appears verbatim in the JSON, so value->path matching cannot find them —
    LLM codegen owns that mapping).
    """
    discovery = Discovery()
    if not sample_jsons:
        return discovery

    # Precompute each sample's leaves once (every field reuses them).
    per_sample_leaves = [list(iter_leaves(obj)) for obj in sample_jsons]

    # Fields non-null across ALL oracles, minus skip_fields. Sorted for stable,
    # deterministic field order in the result.
    candidate_fields = sorted(
        {
            name
            for rec in oracle_records
            for name, value in rec.items()
            if value is not None
        }
        - set(skip_fields)
    )

    for fname in candidate_fields:
        if any(rec.get(fname) is None for rec in oracle_records):
            continue  # null in some sample -> cannot establish a consistent path

        numeric = fname in numeric_fields

        # Sample 0 seeds the candidate (path -> scales) map; later samples intersect.
        consistent = _path_scales_for_value(
            per_sample_leaves[0], oracle_records[0][fname], numeric=numeric
        )
        for leaves, rec in zip(per_sample_leaves[1:], oracle_records[1:]):
            here = _path_scales_for_value(leaves, rec[fname], numeric=numeric)
            # Keep only paths present in BOTH, intersecting their scale sets so the
            # SAME (path, scale) holds across every sample.
            consistent = {
                path: scales & here[path]
                for path, scales in consistent.items()
                if path in here and (scales & here[path])
            }
            if not consistent:
                break

        if not consistent:
            continue

        # Lexicographically-smallest path (stable, generalizing choice).
        best_path = min(consistent, key=_path_sort_key)
        discovery.paths[fname] = list(best_path)
        if numeric:
            # Prefer scale 1 when both somehow match ("leaf already in unit");
            # otherwise the single surviving scale (10000 for a leaf stored in
            # units of ten thousand).
            scales = consistent[best_path]
            discovery.scale[fname] = 1 if 1 in scales else min(scales)

    return discovery


# --------------------------------------------------------------------------- #
# emit_crawler: render a complete, AST-gate-clean crawler module.
#
# The emitted module imports only re/json + crawloop.contract, defines a single
# Crawler class, extracts the FIRST marked JSON island with an inlined regex +
# json.loads, reads each discovered path via an inlined total `_read`, applies the
# per-field numeric scale, and returns one item. It contains NO dunder names and no
# banned calls, so crawloop.safety.ast_check passes it.
# --------------------------------------------------------------------------- #

# The generated module is CODE (a crawler skeleton), not prose, so it lives here as
# a format template rather than in prompts/. Only the marker regex, the class
# identity, and the per-field reads are interpolated; the rest is fixed scaffolding.
_MODULE_TEMPLATE = '''\
"""Auto-generated value-path crawler for {family!r} (deterministic, no LLM).

Generated by crawloop.loop.jsonpath.emit_crawler: each field is read from a
literal path discovered against the oracle, so no navigation logic is guessed.
"""

import re
import json

from crawloop.contract import Crawler, CrawlResult

_BLOB_RE = re.compile(
    r"<script\\b[^>]*(?:{markers})[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


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


def _num(value, scale):
    """Coerce a JSON leaf to a scaled number (int when integral, else float).

    Strings (incl. comma-grouped) are parsed; non-numeric values yield None. An
    integral result is returned as int so whole quantities land as ints; a
    fractional one (e.g. a rating or a measurement) is preserved as float.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    scaled = value * scale
    return int(scaled) if float(scaled).is_integer() else float(scaled)


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
{reads}
        return CrawlResult(items=[item])
'''

# One read line per field. Numeric fields (those with a scale entry) go through
# _num so the leaf is coerced + scaled; string fields are stored verbatim. Both
# guard on None so a missing leaf simply omits the field.
_NUMERIC_READ = (
    "        value = _num(_read(data, {path!r}), {scale})\n"
    "        if value is not None:\n"
    "            item[{field!r}] = value\n"
)
_STRING_READ = (
    "        value = _read(data, {path!r})\n"
    "        if value is not None:\n"
    "            item[{field!r}] = value\n"
)


def _classname(family: str) -> str:
    """A valid, dunder-free Python class name derived from ``family``.

    Keeps only alphanumerics from the family id, title-cases word boundaries, and
    suffixes ``ValuePathCrawler``. A leading digit (or an empty result) is prefixed
    so the identifier is always valid — and it never contains ``__`` so it cannot
    trip the AST gate's dunder rule.
    """
    parts = re.split(r"[^0-9A-Za-z]+", family)
    stem = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not stem or stem[0].isdigit():
        stem = "Family" + stem
    return stem + "ValuePathCrawler"


def emit_crawler(
    *,
    family: str,
    schema_ref: str,
    discovery: Discovery,
    json_markers: list[str],
) -> str:
    """Render a complete crawler module source string for ``discovery``.

    The module imports only ``re`` / ``json`` and ``crawloop.contract`` (so it
    passes :func:`crawloop.safety.ast_check`), defines one :class:`Crawler`
    subclass with the given ``family`` / ``schema_ref``, extracts the FIRST
    ``<script>`` JSON island whose open tag matches any of ``json_markers``, and
    reads each field in ``discovery.paths`` from its literal path. Numeric fields
    (those present in ``discovery.scale``) are run through the scaling coercion; the
    rest are stored verbatim. A field whose path is absent on a given page is
    omitted from the item (its read is None). When no island matches, ``crawl``
    returns ``CrawlResult(items=[])``.

    The marker alternation is regex-escaped, so a marker containing regex
    metacharacters (``application/ld+json`` has a ``+``) is matched literally.
    """
    markers = "|".join(re.escape(m) for m in json_markers)

    reads_lines: list[str] = []
    # Deterministic field order: sorted by field name so the emitted source is
    # stable across runs (and diffs cleanly).
    for fname in sorted(discovery.paths):
        path = discovery.paths[fname]
        if fname in discovery.scale:  # numeric field -> coerce + scale
            reads_lines.append(
                _NUMERIC_READ.format(path=path, scale=discovery.scale[fname], field=fname)
            )
        else:  # string field -> store verbatim
            reads_lines.append(_STRING_READ.format(path=path, field=fname))

    reads = "".join(reads_lines)

    return _MODULE_TEMPLATE.format(
        family=family,
        schema_ref=schema_ref,
        classname=_classname(family),
        markers=markers,
        reads=reads,
    )
