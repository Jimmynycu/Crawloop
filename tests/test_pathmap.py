"""Offline tests for the path-map codegen strategy (Task 1 + Task 2).

The path-map strategy complements the verbatim value->path matcher
(:mod:`crawloop.loop.jsonpath`): listing/detail pages embed the full record as JSON,
but many fields are NORMALIZED — a salary stored in units of ten thousand (``12``)
whose record value is ``120000``, an enum code (``"Full-time"`` -> ``"full_time"``),
or a derived string (``location`` = city + ", " + region). Pure value matching can
never find those, so the LLM emits ONCE a declarative map ``{field: {path, transform}}``
describing where each field lives and how to transform it, and this module's
DETERMINISTIC core executes that map for free at run time.

Everything here is pure + offline: no network, no LLM, no subprocess except the
real sandbox (itself offline — it only ever sees the one stored HTML). The single
LLM step (:func:`propose_fieldmap`) is exercised with a :class:`FakeCompleter`.

Coverage:

* :func:`apply_field_spec` for every transform: none / x10000 / int / float /
  ``{map}`` (substring-tolerant) / ``{concat}`` / ``{list}``, plus the miss-is-None
  contract (a bad path or non-coercible value yields None, never raises).
* :func:`apply_fieldmap` end-to-end: a multi-field map over one JSON object drops
  None-valued fields.
* :func:`emit_crawler`: output passes :func:`crawloop.safety.ast_check` AND,
  run in the real offline sandbox over a synthetic ``__NEXT_DATA__`` page,
  reproduces a record with a x10000 salary, a label-to-code enum map, and a
  concatenated ``location``.
* :func:`propose_fieldmap`: a :class:`FakeCompleter` returning a known map JSON ->
  the parsed map is returned and validates against the sample; a map that
  reproduces too little of the oracle raises.
"""

from __future__ import annotations

import json

import pytest

from crawloop.llm import FakeCompleter
from crawloop.loop.jsonpath import JSON_MARKERS
from crawloop.loop.pathmap import (
    apply_field_spec,
    apply_fieldmap,
    emit_crawler,
    propose_fieldmap,
)
from crawloop.loop.sandbox import run_in_sandbox
from crawloop.safety import ast_check

# A nested page-JSON shaped like a Next.js payload: a salary stored in units of 10k,
# an enum source label, and city/region parts for a concatenated location.
_JSON = {
    "props": {
        "pageProps": {
            "detail": {
                "salaryK": 12,
                "etype": "Full-time",
                "city": "Springfield",
                "region": "IL",
                "headcount": "5",
                "rating": "4.5",
                "logos": [
                    {"url": "https://img/1.png"},
                    {"url": "https://img/2.png"},
                ],
                "skills": ["python", "sql"],
            }
        }
    }
}
_BASE = ["props", "pageProps", "detail"]


# --------------------------------------------------------------------------- #
# apply_field_spec — one assertion per transform in the vocabulary.
# --------------------------------------------------------------------------- #


def test_apply_field_spec_none_returns_value_verbatim():
    """The ``"none"`` transform returns the leaf at the path unchanged."""
    spec = {"path": [*_BASE, "city"], "transform": "none"}
    assert apply_field_spec(_JSON, spec) == "Springfield"


def test_apply_field_spec_x10000_scales_units_as_int():
    """``"x10000"`` multiplies the leaf by 10000 and returns an int —
    a salary stored as 12 -> 120000."""
    spec = {"path": [*_BASE, "salaryK"], "transform": "x10000"}
    result = apply_field_spec(_JSON, spec)
    assert result == 120000
    assert isinstance(result, int)


def test_apply_field_spec_int_and_float_coerce_numeric_strings():
    """``"int"`` / ``"float"`` coerce a numeric STRING leaf to the right type."""
    assert apply_field_spec(_JSON, {"path": [*_BASE, "headcount"], "transform": "int"}) == 5
    rating = apply_field_spec(_JSON, {"path": [*_BASE, "rating"], "transform": "float"})
    assert rating == pytest.approx(4.5)
    assert isinstance(rating, float)


def test_apply_field_spec_map_is_substring_tolerant():
    """A ``{map}`` transform reads the leaf and looks it up; the match is
    substring-tolerant — if the JSON value CONTAINS a key, that key's code wins.
    An unmapped value yields None."""
    # Exact key.
    spec = {
        "path": [*_BASE, "etype"],
        "transform": {"map": {"Full-time": "full_time", "Part-time": "part_time"}},
    }
    assert apply_field_spec(_JSON, spec) == "full_time"

    # Substring tolerance: the JSON stores a longer label that CONTAINS the key.
    j = {"etype": "Permanent Full-time"}
    spec2 = {"path": ["etype"], "transform": {"map": {"Full-time": "full_time"}}}
    assert apply_field_spec(j, spec2) == "full_time"

    # No key is contained in the value -> None.
    spec3 = {"path": [*_BASE, "etype"], "transform": {"map": {"Contract": "contract"}}}
    assert apply_field_spec(_JSON, spec3) is None


def test_apply_field_spec_concat_joins_non_empty_subpaths():
    """A ``{concat}`` transform reads each sub-path from the JSON root and joins the
    non-empty values with ``sep`` (here ", ") -> location."""
    spec = {
        "path": [],
        "transform": {
            "concat": [[*_BASE, "city"], [*_BASE, "region"]],
            "sep": ", ",
        },
    }
    assert apply_field_spec(_JSON, spec) == "Springfield, IL"


def test_apply_field_spec_concat_skips_missing_parts_and_uses_sep():
    """concat drops parts that read None/empty and joins the rest with ``sep``."""
    spec = {
        "path": [],
        "transform": {
            "concat": [[*_BASE, "city"], [*_BASE, "nope"], [*_BASE, "region"]],
            "sep": "-",
        },
    }
    # The missing middle part is skipped; the two present parts join with "-".
    assert apply_field_spec(_JSON, spec) == "Springfield-IL"


def test_apply_field_spec_list_with_field_maps_elements():
    """A ``{list}`` transform with a ``field`` returns ``element[field]`` for each
    element of the array at the path."""
    spec = {"path": [], "transform": {"list": [[*_BASE, "logos"]], "field": "url"}}
    assert apply_field_spec(_JSON, spec) == ["https://img/1.png", "https://img/2.png"]


def test_apply_field_spec_list_without_field_returns_the_array():
    """A ``{list}`` transform with ``field: null`` returns the array of scalars
    at the path verbatim."""
    spec = {"path": [], "transform": {"list": [[*_BASE, "skills"]], "field": None}}
    assert apply_field_spec(_JSON, spec) == ["python", "sql"]


def test_apply_field_spec_returns_none_on_miss_never_raises():
    """A path that misses, or a value that cannot be coerced, yields None rather
    than raising — the contract the emitted crawler relies on to stay total."""
    # Missing path.
    assert apply_field_spec(_JSON, {"path": ["no", "such"], "transform": "none"}) is None
    # Non-numeric value through x10000 / int / float.
    j = {"x": "not-a-number"}
    assert apply_field_spec(j, {"path": ["x"], "transform": "x10000"}) is None
    assert apply_field_spec(j, {"path": ["x"], "transform": "int"}) is None
    assert apply_field_spec(j, {"path": ["x"], "transform": "float"}) is None


# --------------------------------------------------------------------------- #
# apply_fieldmap — full record, dropping None values.
# --------------------------------------------------------------------------- #


def _fieldmap() -> dict:
    """A complete map exercising scalar / scaled / enum / concat / list transforms
    against ``_JSON``, plus one field whose path misses (so it is dropped)."""
    return {
        "salary": {"path": [*_BASE, "salaryK"], "transform": "x10000"},
        "city": {"path": [*_BASE, "city"], "transform": "none"},
        "team_size": {"path": [*_BASE, "headcount"], "transform": "int"},
        "employment_type": {
            "path": [*_BASE, "etype"],
            "transform": {"map": {"Full-time": "full_time"}},
        },
        "location": {
            "path": [],
            "transform": {
                "concat": [[*_BASE, "city"], [*_BASE, "region"]],
                "sep": ", ",
            },
        },
        "skill_list": {
            "path": [],
            "transform": {"list": [[*_BASE, "logos"]], "field": "url"},
        },
        # This one misses on purpose -> dropped from the result.
        "missing": {"path": ["does", "not", "exist"], "transform": "none"},
    }


def test_apply_fieldmap_builds_record_and_drops_none():
    """apply_fieldmap evaluates every field spec and returns a record with the
    None-valued fields dropped."""
    record = apply_fieldmap(_JSON, _fieldmap())
    assert record == {
        "salary": 120000,
        "city": "Springfield",
        "team_size": 5,
        "employment_type": "full_time",
        "location": "Springfield, IL",
        "skill_list": ["https://img/1.png", "https://img/2.png"],
    }
    assert "missing" not in record


# --------------------------------------------------------------------------- #
# emit_crawler — gate-clean + reproduces the record in the real sandbox.
# --------------------------------------------------------------------------- #

# A synthetic detail page carrying the full record as a Next.js JSON island.
_PAGE_HTML = (
    "<!doctype html><html><head><title>x</title></head><body>"
    "<h1>some heading</h1>"
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {"props": {"pageProps": {"detail": _JSON["props"]["pageProps"]["detail"]}}},
        ensure_ascii=False,
    )
    + "</script></body></html>"
)


def test_emit_crawler_passes_ast_gate():
    """The emitted module clears the static AST allowlist: imports only re/json +
    crawloop.contract, no dunder, no banned calls, no str.format."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        json_markers=JSON_MARKERS,
        fieldmap=_fieldmap(),
    )
    assert ast_check(source) == []
    ast_check(source, raise_on_violation=True)
    assert "import re" in source
    assert "import json" in source
    assert "from crawloop.contract import" in source
    # House security rule: no str.format anywhere in the emitted source.
    assert ".format(" not in source


def test_emit_crawler_reproduces_record_in_sandbox():
    """Running the emitted crawler in the REAL offline sandbox over the synthetic
    page reproduces the record — including the x10000 salary, the enum mapped from a
    source label, and the concatenated location."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        json_markers=JSON_MARKERS,
        fieldmap=_fieldmap(),
    )
    items = run_in_sandbox(source, _PAGE_HTML, url="https://jobs.example.com/posting/X")
    assert len(items) == 1
    item = items[0]
    assert item["salary"] == 120000  # 12 * 10000 (x10000 transform)
    assert item["employment_type"] == "full_time"  # Full-time -> full_time (enum map)
    assert item["location"] == "Springfield, IL"  # concat
    assert item["team_size"] == 5
    assert item["skill_list"] == ["https://img/1.png", "https://img/2.png"]


def test_emit_crawler_returns_empty_when_no_blob():
    """A page with NO JSON island matching the markers yields CrawlResult(items=[])
    rather than crashing."""
    source = emit_crawler(
        family="jobs.example.com/posting",
        schema_ref="JobPosting@1",
        json_markers=JSON_MARKERS,
        fieldmap=_fieldmap(),
    )
    items = run_in_sandbox(source, "<html><body>no json here</body></html>")
    assert items == []


# --------------------------------------------------------------------------- #
# propose_fieldmap — the single LLM step, exercised with a FakeCompleter.
# --------------------------------------------------------------------------- #


def _oracle_record() -> dict:
    """The trusted record for ``_PAGE_HTML`` — the normalized values the path-map
    must reproduce from the embedded JSON."""
    return {
        "salary": 120000,
        "city": "Springfield",
        "team_size": 5,
        "employment_type": "full_time",
        "location": "Springfield, IL",
    }


async def test_propose_fieldmap_returns_and_validates_map():
    """A completer scripted to return a known map JSON -> propose_fieldmap parses
    it (tolerating a ```json fence), validates it reproduces the oracle from the
    sample's own JSON, and returns it."""
    fieldmap = {
        k: v for k, v in _fieldmap().items() if k not in ("missing", "skill_list")
    }
    # The model wraps its JSON in a code fence (propose_fieldmap must strip it).
    completer = FakeCompleter(
        ["```json\n" + json.dumps(fieldmap, ensure_ascii=False) + "\n```"]
    )

    returned = await propose_fieldmap(
        _PAGE_HTML, _oracle_record(), "JobPosting@1", completer,
        model="anthropic/claude-fable-5",
    )
    assert returned == fieldmap
    # Exactly one model call was made.
    assert len(completer.calls) == 1
    # And the returned map really reproduces the oracle on the sample's JSON.
    assert apply_fieldmap(_JSON, returned) == _oracle_record()


async def test_propose_fieldmap_raises_when_map_reproduces_too_little():
    """A syntactically valid map that reproduces too few of the oracle's populated
    fields raises (so the caller falls back to other candidates)."""
    # Only 1 of 5 oracle fields reproduced (city) -> below the 60% bar.
    poor = {"city": {"path": [*_BASE, "city"], "transform": "none"}}
    completer = FakeCompleter([json.dumps(poor, ensure_ascii=False)])
    with pytest.raises(Exception):
        await propose_fieldmap(
            _PAGE_HTML, _oracle_record(), "JobPosting@1", completer,
            model="anthropic/claude-fable-5",
        )


async def test_propose_fieldmap_raises_on_non_json_completion():
    """A completion that is not JSON at all raises rather than returning garbage."""
    completer = FakeCompleter(["I cannot produce a map for this page."])
    with pytest.raises(Exception):
        await propose_fieldmap(
            _PAGE_HTML, _oracle_record(), "JobPosting@1", completer,
            model="anthropic/claude-fable-5",
        )
