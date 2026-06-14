"""The deterministic-core + LLM-tail hybrid: close the completeness gap, cheaply.

A promoted deterministic crawler (the value-path / path-map strategies in
:mod:`crawloop.loop`) is EXACT and FREE on the high-value CORE fields, but on a
wide schema it systematically leaves a few NORMALIZED / INFERRED "tail" fields blank
— a mapped enum, a concatenated ``full_address``, a scaled price, an inferred usage.
The per-page T2 LLM (:func:`crawloop.fallback.direct_extract`) gets those, but at
full per-page cost. The hybrid closes the gap WITHOUT that cost:

1. Run the deterministic crawler (free) -> most fields.
2. For ONLY the fields it systematically leaves blank — the family's *residual set*,
   computed once at promote time from the winner's sandbox output vs the oracles —
   make ONE small, targeted LLM call (:func:`fill_residual`) extracting just those.
3. Merge (:func:`merge_record`): the deterministic value wins where present; the LLM
   tail fills only the gaps.

The cost guarantee falls straight out of the residual set: when it is empty NO LLM
call happens (``$0``); otherwise exactly ONE small call per page, prompting only the
residual fields. This module is three pieces:

* :func:`compute_residual_fields` — PURE. Derives the residual set from the winning
  candidate's per-sample outputs vs the oracles (both already in the driver at
  promote). A field is residual iff the oracle populates it on >= 1 sample AND the
  crawler left it null/missing on EVERY sample where the oracle had it.
* :func:`fill_residual` — the ONE LLM call. Builds a focused prompt (the trimmed page
  + a projection of the schema to just the residual fields), parses the JSON object,
  validates each value against its field's type (dropping ones that don't fit), and
  returns ``{field: value}``. On ANY failure returns ``{}`` — it never raises, so the
  deterministic record always stands.
* :func:`merge_record` — PURE. Deterministic values win where non-null; the tail only
  fills keys missing/null in the deterministic record.

Prompt text lives in ``prompts/residual_system.txt`` / ``prompts/residual_user.txt``
(house rule: long-form prose is not inlined in code).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

from pydantic import TypeAdapter

from crawloop.htmlutil import trim_html
from crawloop.llm import Completer
from crawloop.schemas import get_schema, schema_json

# Prompt files live in the top-level prompts/ dir (sibling of the package), the same
# convention fallback.py / pathmap.py use. Loaded ONCE at import: they are static.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM = (_PROMPTS_DIR / "residual_system.txt").read_text(encoding="utf-8")
USER_TEMPLATE = Template((_PROMPTS_DIR / "residual_user.txt").read_text(encoding="utf-8"))

# Strips an opening ```/```json fence (and the closing ```) some models wrap JSON
# in despite being told not to. Tolerant of surrounding whitespace.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# compute_residual_fields — pure
# --------------------------------------------------------------------------- #


def _record_has(record: dict, field: str) -> bool:
    """Whether ``field`` is present AND non-null in one record.

    A present-but-``None`` value reads as *missing* (the same convention the
    validator's fill-rate uses): a crawler that emits ``None`` for a field it can no
    longer locate has not filled it.
    """
    return record.get(field) is not None


def _any_record_has(records: list[dict], field: str) -> bool:
    """Whether any record in a per-sample list populates ``field`` (non-null)."""
    return any(_record_has(r, field) for r in records)


def compute_residual_fields(
    crawler_outputs: list[list[dict]],
    oracle_records: list[list[dict]],
    schema_ref: str,
) -> list[str]:
    """The family's residual set: fields the oracle fills that the crawler never does.

    ``crawler_outputs`` and ``oracle_records`` are aligned per sample (each a list of
    records, since a page may carry several). A schema field is *residual* iff:

    * the ORACLE populates it (non-null) on >= 1 sample, AND
    * the crawler left it null/missing on EVERY sample where the oracle had it.

    That is the "systematically blind" signal: the deterministic crawler is not just
    occasionally short on the field, it never reaches it on any page the ground truth
    says it should. Fields both sides fill, fields neither side fills, and fields the
    crawler gets on even one such sample are all excluded. Only declared schema fields
    are considered (a stray oracle key is ignored — it can't be projected or
    validated downstream). Returns a STABLE sorted list, so the persisted residual set
    is deterministic across runs.

    Pure: no I/O, no model. The driver calls this at promote time with the winning
    candidate's sandbox outputs and the usable oracles it already has in hand.
    """
    model = get_schema(schema_ref)
    fields = list(model.model_fields)

    residual: list[str] = []
    for field in fields:
        # Samples where the oracle populated this field — the pages on which the
        # crawler *should* have produced it.
        oracle_samples = [
            i for i, records in enumerate(oracle_records) if _any_record_has(records, field)
        ]
        if not oracle_samples:
            continue  # the oracle never has it -> no ground truth to fill toward.
        # Residual only if the crawler missed it on EVERY such sample. (An index
        # past the crawler-output list, e.g. a dropped sample, reads as missing.)
        crawler_missed_all = all(
            i >= len(crawler_outputs) or not _any_record_has(crawler_outputs[i], field)
            for i in oracle_samples
        )
        if crawler_missed_all:
            residual.append(field)
    return sorted(residual)


# --------------------------------------------------------------------------- #
# fill_residual — the ONE targeted LLM call
# --------------------------------------------------------------------------- #


def _resolve_ref(ref: str, defs: dict) -> dict:
    """Resolve a local ``#/$defs/Name`` ref to its definition (``{}`` if absent)."""
    name = ref.rsplit("/", 1)[-1]
    return defs.get(name, {})


def _project_field_schema(field: str, full: dict) -> dict:
    """A compact, self-contained schema for ONE field, enum ``$ref``s inlined.

    Pulls ``properties[field]`` out of the model's full JSON schema and, for any
    ``$ref`` into ``$defs`` (the enum members), inlines the referenced definition so
    the model sees the allowed enum codes directly rather than a dangling ``$ref``.
    Keeps the projection small (only the requested field, no global ``$defs`` blob),
    which is the whole point of the residual prompt being cheap.
    """
    defs = full.get("$defs", {})
    spec = dict(full.get("properties", {}).get(field, {}))
    # Pydantic renders `X | None` as anyOf[ {ref/type}, {null} ]; inline any $ref so
    # the enum codes are visible inline.
    if "anyOf" in spec:
        spec["anyOf"] = [
            _resolve_ref(opt["$ref"], defs) if "$ref" in opt else opt
            for opt in spec["anyOf"]
        ]
    elif "$ref" in spec:
        spec = _resolve_ref(spec["$ref"], defs)
    return spec


def _project_schema(residual_fields: list[str], schema_ref: str) -> dict:
    """``{field: compact-field-schema}`` for just the residual fields."""
    full = schema_json(schema_ref)
    return {field: _project_field_schema(field, full) for field in residual_fields}


def _field_adapters(residual_fields: list[str], schema_ref: str) -> dict[str, TypeAdapter]:
    """A :class:`TypeAdapter` per residual field, keyed by name.

    Each adapter validates a single value against that field's own annotation
    (including ``Enum`` members and ``X | None`` unions), so :func:`fill_residual` can
    accept/reject each returned value individually without constructing the whole
    record (which would trip ``extra="forbid"`` and the other required fields).
    """
    model = get_schema(schema_ref)
    out: dict[str, TypeAdapter] = {}
    for field in residual_fields:
        info = model.model_fields.get(field)
        if info is not None:
            out[field] = TypeAdapter(info.annotation)
    return out


def _is_blank(value: Any) -> bool:
    """Whether a value is "not a fill": None, or an empty/whitespace-only string."""
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_object(raw: str) -> dict:
    """Parse a model completion into a JSON object, or raise ``ValueError``.

    Strips an optional ```json fence and enforces that the top level is an object —
    the residual prompt asks for ``{field: value}``, so an array or scalar is the
    wrong shape and is rejected (the caller turns any failure into ``{}``).
    """
    text = _FENCE_RE.sub("", raw).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


async def fill_residual(
    html: str,
    residual_fields: list[str],
    schema_ref: str,
    completer: Completer,
    model: str = "anthropic/claude-fable-5",
    *,
    fetch_trim: bool = True,
) -> dict:
    """ONE targeted LLM call extracting ONLY ``residual_fields`` from ``html``.

    Builds a focused prompt — the (optionally trimmed) page plus a projection of the
    schema to just the residual fields (their names + types + enum codes) — asks the
    model for a JSON object with only those keys, parses it (stripping a code fence),
    and validates each value against its field's type via the schema. A value that
    fails its type, or that is null/blank, is DROPPED; a key outside ``residual_fields``
    is ignored. Returns ``{field: value}`` for the values that survived.

    The cost contract: when ``residual_fields`` is empty this returns ``{}`` and makes
    NO model call at all. Otherwise it makes EXACTLY ONE :meth:`Completer.complete`
    call, prompting only the residual fields.

    Never raises. Unparseable output, the wrong JSON shape, or even the completer
    itself raising all yield ``{}`` — the deterministic record the caller already has
    must always stand, so a tail-fill failure can never break a request.

    ``fetch_trim`` controls whether the page is reduced via
    :func:`~crawloop.htmlutil.trim_html` (the default — same cheap, JSON-hoisting
    reduction the T2 extractor uses) before being sent; pass ``False`` to send it raw.
    """
    if not residual_fields:
        return {}

    try:
        fields_schema = _project_schema(residual_fields, schema_ref)
        adapters = _field_adapters(residual_fields, schema_ref)
        page = trim_html(html) if fetch_trim else html
        user = USER_TEMPLATE.safe_substitute(
            fields_json=json.dumps(fields_schema, ensure_ascii=False, indent=2),
            html=page,
        )
        raw = await completer.complete(system=SYSTEM, user=user, model=model)
        parsed = _parse_object(raw)
    except Exception:  # noqa: BLE001 — tail-fill is best-effort; deterministic stands
        return {}

    out: dict = {}
    for field in residual_fields:
        if field not in parsed:
            continue
        value = parsed[field]
        if _is_blank(value):
            continue  # a null/blank value is not a fill.
        adapter = adapters.get(field)
        if adapter is None:
            continue
        try:
            coerced = adapter.validate_python(value)
            # Re-serialise the validated value to a JSON-NATIVE form: this both
            # confirms the type and normalises it — an enum member becomes its CODE
            # string ("condo", not PropertyType.CONDO), and a numeric/bool the model
            # sent as a string ("31350000"/"true") becomes the proper int/bool. So the
            # merged record never carries a string where the schema wants a number, and
            # the value matches the deterministic crawler's / oracle's JSON form.
            out[field] = adapter.dump_python(coerced, mode="json")
        except Exception:  # noqa: BLE001 — bad value (or unserialisable) -> drop it
            continue
    return out


# --------------------------------------------------------------------------- #
# merge_record — pure
# --------------------------------------------------------------------------- #


def merge_record(deterministic: dict, llm_tail: dict) -> dict:
    """Merge the deterministic record with the LLM tail-fill; deterministic wins.

    A deterministic value that is present and non-null is kept verbatim — the tail
    NEVER overwrites it (the deterministic crawler is exact on what it does produce).
    The tail only fills a key the deterministic record is missing or has as ``None``.
    Pure: returns a new dict and mutates neither input.
    """
    merged = dict(deterministic)
    for field, value in llm_tail.items():
        if merged.get(field) is None:
            merged[field] = value
    return merged
