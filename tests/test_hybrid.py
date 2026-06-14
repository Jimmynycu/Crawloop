"""Tests for the deterministic-core + LLM-tail hybrid (Wave 3): ``crawloop.hybrid``.

The hybrid closes the completeness gap a free, deterministic crawler systematically
leaves on wide schemas (mapped enums, concatenated ``location``, scaled salaries,
inferred fields) WITHOUT paying full per-page LLM cost: the deterministic crawler gets
most fields for free, and ONE small targeted LLM call fills only the family's known
"residual set". These tests cover the three pure/LLM helpers in isolation (offline,
``FakeCompleter`` only); the engine integration is in ``tests/test_engine_hybrid.py``.

* :func:`compute_residual_fields` — pure. A field is residual iff the ORACLE
  populates it on >= 1 sample AND the crawler left it null/missing on EVERY sample
  where the oracle had it. Fields both sides fill, or neither fills, are not residual.
* :func:`merge_record` — deterministic values win where non-null; the LLM tail only
  fills keys missing/null in the deterministic record, never overwrites.
* :func:`fill_residual` — ONE LLM call extracting only the residual fields; the
  response is parsed (fences stripped) and each value validated against its field's
  type (bad values dropped). Any failure returns ``{}`` so the deterministic record
  still stands.
"""

from __future__ import annotations

import pytest

from crawloop.hybrid import compute_residual_fields, fill_residual, merge_record
from crawloop.llm import FakeCompleter

SCHEMA = "JobPosting@1"


# --------------------------------------------------------------------------- #
# compute_residual_fields — pure
# --------------------------------------------------------------------------- #


def test_residual_is_oracle_only_field_missing_on_every_sample():
    """A field the oracle fills but the crawler never does is residual.

    ``employment_type`` / ``location`` are the mapped-enum / concatenated fields
    the deterministic value-path crawler cannot reach: the oracle has them on every
    sample, the crawler has them on none -> both returned, stable-sorted.
    """
    crawler_outputs = [
        [{"title": "A", "salary": 120000}],
        [{"title": "B", "salary": 90000}],
    ]
    oracle_records = [
        [{"title": "A", "salary": 120000, "employment_type": "full_time",
          "location": "Springfield, IL"}],
        [{"title": "B", "salary": 90000, "employment_type": "part_time",
          "location": "Ogdenville, IL"}],
    ]
    assert compute_residual_fields(crawler_outputs, oracle_records, SCHEMA) == [
        "employment_type",
        "location",
    ]


def test_field_both_sides_have_is_not_residual():
    """A field the crawler already fills (matching the oracle) is NOT residual.

    ``title`` / ``salary`` are present on both sides on every sample, so the
    hybrid must not waste the LLM prompt on them — only the genuinely missing
    ``employment_type`` is residual.
    """
    crawler_outputs = [
        [{"title": "A", "salary": 120000}],
        [{"title": "B", "salary": 90000}],
    ]
    oracle_records = [
        [{"title": "A", "salary": 120000, "employment_type": "full_time"}],
        [{"title": "B", "salary": 90000, "employment_type": "part_time"}],
    ]
    assert compute_residual_fields(crawler_outputs, oracle_records, SCHEMA) == [
        "employment_type"
    ]


def test_field_neither_side_has_is_not_residual():
    """A field absent from the oracle too is NOT residual (nothing to fill).

    The oracle never populated ``company`` on any sample, so even though the
    crawler lacks it there is no ground truth to say it should be filled -> excluded.
    """
    crawler_outputs = [[{"title": "A"}], [{"title": "B"}]]
    oracle_records = [
        [{"title": "A", "employment_type": "full_time"}],
        [{"title": "B", "employment_type": "part_time"}],
    ]
    out = compute_residual_fields(crawler_outputs, oracle_records, SCHEMA)
    assert "company" not in out
    assert out == ["employment_type"]


def test_field_crawler_has_on_some_samples_is_not_residual():
    """Residual requires the crawler to miss the field on EVERY sample the oracle has it.

    If the crawler got ``employment_type`` on even one of the samples where the oracle
    had it, the deterministic crawler is NOT systematically blind to it -> not residual.
    """
    crawler_outputs = [
        [{"title": "A", "employment_type": "full_time"}],  # crawler got it here
        [{"title": "B"}],  # missed here
    ]
    oracle_records = [
        [{"title": "A", "employment_type": "full_time"}],
        [{"title": "B", "employment_type": "part_time"}],
    ]
    assert compute_residual_fields(crawler_outputs, oracle_records, SCHEMA) == []


def test_null_in_crawler_counts_as_missing():
    """A field present-but-None in the crawler output counts as missing (residual)."""
    crawler_outputs = [[{"title": "A", "employment_type": None}]]
    oracle_records = [[{"title": "A", "employment_type": "full_time"}]]
    assert compute_residual_fields(crawler_outputs, oracle_records, SCHEMA) == [
        "employment_type"
    ]


def test_empty_inputs_yield_no_residuals():
    """No samples / empty records -> empty residual set (no LLM call downstream)."""
    assert compute_residual_fields([], [], SCHEMA) == []
    assert compute_residual_fields([[]], [[]], SCHEMA) == []


def test_unknown_field_in_oracle_is_ignored():
    """A key not on the schema is never residual (it can't be projected/validated)."""
    crawler_outputs = [[{"title": "A"}]]
    oracle_records = [[{"title": "A", "not_a_real_field": "x"}]]
    assert compute_residual_fields(crawler_outputs, oracle_records, SCHEMA) == []


# --------------------------------------------------------------------------- #
# merge_record — deterministic wins, tail fills gaps, never overwrites
# --------------------------------------------------------------------------- #


def test_merge_tail_fills_only_missing_keys():
    det = {"title": "A", "salary": 120000}
    tail = {"employment_type": "full_time", "location": "Springfield, IL"}
    assert merge_record(det, tail) == {
        "title": "A",
        "salary": 120000,
        "employment_type": "full_time",
        "location": "Springfield, IL",
    }


def test_merge_deterministic_value_wins_over_tail():
    """A non-null deterministic value is NEVER overwritten by the tail."""
    det = {"title": "real", "employment_type": "full_time"}
    tail = {"title": "hallucinated", "employment_type": "part_time"}
    assert merge_record(det, tail) == {"title": "real", "employment_type": "full_time"}


def test_merge_tail_fills_null_deterministic_value():
    """A None in the deterministic record is a gap the tail may fill."""
    det = {"title": "A", "employment_type": None}
    tail = {"employment_type": "full_time"}
    assert merge_record(det, tail) == {"title": "A", "employment_type": "full_time"}


def test_merge_empty_tail_returns_deterministic_unchanged():
    det = {"title": "A", "salary": 120000}
    assert merge_record(det, {}) == det


def test_merge_does_not_mutate_inputs():
    det = {"title": "A"}
    tail = {"employment_type": "full_time"}
    merge_record(det, tail)
    assert det == {"title": "A"}
    assert tail == {"employment_type": "full_time"}


# --------------------------------------------------------------------------- #
# fill_residual — ONE LLM call, validated subset; failure -> {}
# --------------------------------------------------------------------------- #


async def test_fill_residual_returns_validated_subset():
    """A well-formed residual JSON yields exactly the residual fields, validated."""
    completer = FakeCompleter(
        ['{"employment_type": "full_time", "location": "Springfield, IL"}']
    )
    out = await fill_residual(
        "<html><script type='application/json'>{}</script></html>",
        ["employment_type", "location"],
        SCHEMA,
        completer,
        model="anthropic/claude-fable-5",
    )
    assert out == {"employment_type": "full_time", "location": "Springfield, IL"}
    # Exactly ONE LLM call.
    assert len(completer.calls) == 1


async def test_fill_residual_strips_code_fence():
    completer = FakeCompleter(['```json\n{"employment_type": "full_time"}\n```'])
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {"employment_type": "full_time"}


async def test_fill_residual_drops_invalid_values():
    """A value that fails its field's type is dropped; valid ones survive.

    ``employment_type`` "not_a_type" is not an enum member -> dropped; ``location``
    is a valid string -> kept. The deterministic record stands for the dropped field.
    """
    completer = FakeCompleter(
        ['{"employment_type": "not_a_type", "location": "Springfield, IL"}']
    )
    out = await fill_residual(
        "<html></html>", ["employment_type", "location"], SCHEMA, completer
    )
    assert out == {"location": "Springfield, IL"}


async def test_fill_residual_ignores_keys_outside_residual_set():
    """Only the requested residual keys are returned, even if the model adds more."""
    completer = FakeCompleter(
        ['{"employment_type": "full_time", "title": "leaked", "bogus": 1}']
    )
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {"employment_type": "full_time"}


async def test_fill_residual_malformed_json_returns_empty():
    """Unparseable model output -> {} (never raises; deterministic record stands)."""
    completer = FakeCompleter(["this is not json at all"])
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {}
    assert len(completer.calls) == 1


async def test_fill_residual_non_object_json_returns_empty():
    """A JSON array (not an object) -> {} (wrong shape, never raises)."""
    completer = FakeCompleter(['["full_time"]'])
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {}


async def test_fill_residual_empty_field_list_makes_no_call():
    """No residual fields -> no LLM call at all (the $0 guarantee)."""
    completer = FakeCompleter([])  # would raise if called
    out = await fill_residual("<html></html>", [], SCHEMA, completer)
    assert out == {}
    assert completer.calls == []


async def test_fill_residual_completer_failure_returns_empty():
    """If the completer itself raises, fill_residual swallows it and returns {}."""

    class BoomCompleter:
        def __init__(self):
            self.calls: list[dict] = []

        async def complete(self, *, system, user, model):
            self.calls.append({"system": system, "user": user, "model": model})
            raise RuntimeError("model exploded")

    completer = BoomCompleter()
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {}
    assert len(completer.calls) == 1


@pytest.mark.parametrize("dropped_value", [None, "", "   "])
async def test_fill_residual_drops_null_and_blank_values(dropped_value):
    """A null/blank value for a residual field is dropped (it is not a fill)."""
    import json

    completer = FakeCompleter([json.dumps({"location": dropped_value})])
    out = await fill_residual(
        "<html></html>", ["location"], SCHEMA, completer
    )
    assert out == {}


async def test_fill_residual_coerces_value_to_json_native_type():
    """A numeric/bool field the model sent as a STRING is coerced to its proper type.

    The merged record must never carry a string where the schema wants a number/bool:
    "120000" -> 120000 (int), "true" -> True (bool). Stored JSON-native so it
    matches the deterministic crawler's / oracle's form and survives re-validation.
    """
    completer = FakeCompleter(
        ['{"salary": "120000", "remote": "true"}']
    )
    out = await fill_residual(
        "<html></html>", ["remote", "salary"], SCHEMA, completer
    )
    assert out == {"salary": 120000, "remote": True}
    assert isinstance(out["salary"], int)
    assert out["remote"] is True


async def test_fill_residual_enum_value_kept_as_code_string():
    """An enum field is stored as its CODE STRING, not a pydantic enum member.

    The value must be JSON-native (the records flow as plain dicts through history /
    audit / the validator), so ``employment_type`` comes back as the string ``"full_time"``.
    """
    completer = FakeCompleter(['{"employment_type": "full_time"}'])
    out = await fill_residual(
        "<html></html>", ["employment_type"], SCHEMA, completer
    )
    assert out == {"employment_type": "full_time"}
    assert isinstance(out["employment_type"], str)
