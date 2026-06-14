"""Task 5.3 — the audit log (SQLite rows + a mirrored JSONL file).

``write_audit`` persists an audit entry to the ``audit`` table AND appends one
JSON line to ``crawlers_dir/audit.jsonl``. ``read_audit`` returns rows
newest-first, optionally filtered by family. The JSONL mirror is the
human-/grep-friendly trail referenced in design §9. Tests use a real DB and a
real tmp file, pinning ``now`` for deterministic timestamps.
"""

from __future__ import annotations

import json

import pytest

from crawloop.registry import Registry


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


def test_write_then_read_round_trips(registry):
    registry.write_audit(
        "promote",
        family="books.toscrape.com/product_list",
        data={"from_version": 2, "to_version": 3},
        now="2026-06-13T00:00:00+00:00",
    )
    entries = registry.read_audit()
    assert len(entries) == 1
    e = entries[0]
    assert e["event"] == "promote"
    assert e["family"] == "books.toscrape.com/product_list"
    assert e["ts"] == "2026-06-13T00:00:00+00:00"
    # data round-trips back to a dict (stored as JSON text).
    assert e["data"] == {"from_version": 2, "to_version": 3}


def test_read_is_newest_first(registry):
    registry.write_audit("a", data={}, now="2026-06-13T00:00:01+00:00")
    registry.write_audit("b", data={}, now="2026-06-13T00:00:02+00:00")
    registry.write_audit("c", data={}, now="2026-06-13T00:00:03+00:00")
    events = [e["event"] for e in registry.read_audit()]
    assert events == ["c", "b", "a"]


def test_read_filters_by_family(registry):
    registry.write_audit("x", family="fam.a/p", data={})
    registry.write_audit("y", family="fam.b/p", data={})
    registry.write_audit("z", family="fam.a/p", data={})
    only_a = registry.read_audit(family="fam.a/p")
    assert [e["event"] for e in only_a] == ["z", "x"]
    assert all(e["family"] == "fam.a/p" for e in only_a)


def test_jsonl_mirror_exists_with_correct_line_count(registry, tmp_path):
    registry.write_audit("a", data={"i": 1})
    registry.write_audit("b", family="fam/p", data={"i": 2})
    registry.write_audit("c", data={"i": 3})
    mirror = tmp_path / "crawlers" / "audit.jsonl"
    assert mirror.exists()
    lines = mirror.read_text().splitlines()
    # One line per write_audit call.
    assert len(lines) == 3
    # Each line is valid JSON carrying the §9 shape (ts/event/family/data).
    first = json.loads(lines[0])
    assert first["event"] == "a"
    assert first["data"] == {"i": 1}
    assert "ts" in first and "family" in first
    # The family-tagged write preserved its family in the mirror.
    second = json.loads(lines[1])
    assert second["family"] == "fam/p"


def test_jsonl_mirror_appends_does_not_truncate(registry, tmp_path):
    """A second write must append, never rewrite the file (audit is append-only)."""
    registry.write_audit("first", data={})
    registry.write_audit("second", data={})
    mirror = tmp_path / "crawlers" / "audit.jsonl"
    lines = mirror.read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["first", "second"]
