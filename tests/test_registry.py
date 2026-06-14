"""Task 5.1 — registry schema, family/version CRUD, version ladder, rollback.

Every test uses a REAL sqlite3 database in ``:memory:`` and a real on-disk
``crawlers_dir`` under pytest's ``tmp_path``, so we assert against actual files
and actual rows — no mocking of the storage layer. Timestamps are pinned via the
``now=`` parameter so ordering/value assertions are deterministic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crawloop.registry import Registry, family_dir, slug
from crawloop.safety import ASTViolation

# A small, valid generated crawler mirroring docs/design.html §5. It only imports
# from the allowlist (parsel, crawloop.contract) and uses no banned construct,
# so the AST gate must accept it.
BOOKS_SOURCE = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksToscrapeProductList(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(html)
        items = []
        for card in sel.css("article.product_pod"):
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
            })
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''

# Source with a banned import (os) — the AST gate must reject it BEFORE any file
# is written to disk.
MALICIOUS_SOURCE = '''\
import os
from crawloop.contract import Crawler, CrawlResult, FetchContext


class Bad(Crawler):
    family = "evil.example.com/x"
    schema_ref = "X@1"

    async def crawl(self, url, ctx):
        os.system("rm -rf /")
        return CrawlResult(items=[])
'''

FAMILY = "books.toscrape.com/product_list"


@pytest.fixture
def registry(tmp_path):
    """A fresh in-memory Registry with a real tmp crawlers_dir per test."""
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


# --------------------------------------------------------------------------- #
# slug — pure, module-level
# --------------------------------------------------------------------------- #


def test_slug_replaces_dots_and_slashes():
    assert slug("books.toscrape.com/product_list") == "books_toscrape_com__product_list"


def test_slug_is_pure_and_stable():
    # Same input -> same output, no hidden state.
    assert slug("a.b/c") == slug("a.b/c") == "a_b__c"


def test_slug_strips_unsafe_path_characters():
    # Characters that are not alnum/_/dot/slash must not survive into a filesystem
    # path (no traversal, no spaces, no shell metacharacters).
    out = slug("a b/../c;rm -rf$")
    assert "/" not in out
    assert " " not in out
    assert ".." not in out
    assert ";" not in out
    assert "$" not in out


# --------------------------------------------------------------------------- #
# M1 — empty / all-stripped family name must never name the crawlers_dir root
# --------------------------------------------------------------------------- #


def test_slug_rejects_empty_family():
    with pytest.raises(ValueError):
        slug("")


def test_slug_rejects_all_stripped_family():
    # A name made entirely of characters that strip away (so the slug would be "")
    # must raise rather than silently produce a root-level path component. Spaces
    # are not structural separators, so they are stripped to nothing.
    with pytest.raises(ValueError):
        slug("   ")


def test_family_dir_rejects_empty_family():
    with pytest.raises(ValueError):
        family_dir("")


def test_add_version_rejects_empty_family_before_writing(registry, tmp_path):
    with pytest.raises(ValueError):
        registry.add_version("", BOOKS_SOURCE)
    # Nothing may have been written to the crawlers tree (and certainly not its
    # root).
    assert list((tmp_path / "crawlers").rglob("*.py")) == []


# --------------------------------------------------------------------------- #
# I1 — family_dir is injective: colliding slugs get DISTINCT on-disk dirs
# --------------------------------------------------------------------------- #

# These four families collapse to only TWO readable slugs ("a__b" for a/b & a__b,
# "a_b" for a.b & a_b) — two genuine slug collisions. Only family_dir's raw-name
# hash suffix keeps all four apart on disk.
_COLLIDING = ["a/b", "a__b", "a.b", "a_b"]


def _marked_source(marker: str) -> str:
    """A valid, AST-clean crawler whose schema_ref carries a unique marker, so a
    loaded instance reveals WHICH family's file actually backed it."""
    return (
        "from crawloop.contract import Crawler, CrawlResult, FetchContext\n\n\n"
        "class Marked(Crawler):\n"
        '    family = "marker.example.com/x"\n'
        f'    schema_ref = "{marker}"\n\n'
        "    async def crawl(self, url, ctx):\n"
        "        return CrawlResult(items=[])\n"
    )


def test_family_dir_is_injective_for_colliding_slugs():
    # The slugs genuinely collide (fewer distinct slugs than families)...
    assert len({slug(f) for f in _COLLIDING}) < len(_COLLIDING)
    # ...but family_dir keeps every distinct family in its own directory.
    dirs = {family_dir(f) for f in _COLLIDING}
    assert len(dirs) == len(_COLLIDING)


def test_colliding_families_get_distinct_files_no_overwrite(registry, tmp_path):
    # Register v1 for each colliding family with a DISTINCT marker source.
    for f in _COLLIDING:
        registry.add_version(f, _marked_source(f"M::{f}"))
    # Every family wrote to its own file (no overwrite): four distinct paths,
    # all present on disk, each in a different directory.
    paths = []
    for f in _COLLIDING:
        row = registry._versions_row(f, 1)
        assert row is not None
        p = Path(row["path"])
        assert p.exists()
        paths.append(p)
    assert len({str(p) for p in paths}) == len(_COLLIDING)
    assert len({p.parent for p in paths}) == len(_COLLIDING)
    # The actual .py files written equal the number of families (none clobbered).
    assert len(list((tmp_path / "crawlers").rglob("*.py"))) == len(_COLLIDING)


def test_loading_colliding_families_returns_each_own_crawler(registry):
    for f in _COLLIDING:
        n = registry.add_version(f, _marked_source(f"M::{f}"))
        registry.set_active(f, n)
    # Each family loads ITS OWN source (proven by the per-family marker), so no
    # family was overwritten by a slug-collision.
    for f in _COLLIDING:
        crawler = registry.load_crawler(f)
        assert crawler.schema_ref == f"M::{f}"


# --------------------------------------------------------------------------- #
# upsert_family / get_family
# --------------------------------------------------------------------------- #


def test_get_family_unknown_returns_none(registry):
    assert registry.get_family("nope.example.com/x") is None


def test_upsert_family_roundtrip(registry):
    registry.upsert_family(FAMILY, ["^https?://books\\.toscrape\\.com/.*$"], "Product@1")
    row = registry.get_family(FAMILY)
    assert row is not None
    assert row["family"] == FAMILY
    assert row["schema_ref"] == "Product@1"
    # url_patterns round-trips back to a Python list (stored as JSON text).
    assert row["url_patterns"] == ["^https?://books\\.toscrape\\.com/.*$"]
    assert row["status"] == "healthy"
    assert row["created_at"] is not None


def test_upsert_family_updates_existing_in_place(registry):
    registry.upsert_family(FAMILY, ["^a$"], "Product@1")
    registry.upsert_family(FAMILY, ["^b$", "^c$"], "Product@2")
    row = registry.get_family(FAMILY)
    assert row["url_patterns"] == ["^b$", "^c$"]
    assert row["schema_ref"] == "Product@2"
    # Still exactly one row for the family (upsert, not insert-duplicate).
    assert registry.get_family(FAMILY) is not None


# --------------------------------------------------------------------------- #
# add_version — AST gate BEFORE write, file written, sha, n increment
# --------------------------------------------------------------------------- #


def test_add_version_rejects_ungated_source_before_writing_any_file(registry, tmp_path):
    registry.upsert_family("evil.example.com/x", ["^x$"], "X@1")
    with pytest.raises(ASTViolation):
        registry.add_version("evil.example.com/x", MALICIOUS_SOURCE)
    # CRITICAL: no file may have been written for the rejected source. The whole
    # crawlers tree must contain zero .py files.
    py_files = list((tmp_path / "crawlers").rglob("*.py"))
    assert py_files == []


def test_add_version_writes_file_and_returns_n(registry, tmp_path):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    assert n == 1
    # On-disk dir is the INJECTIVE family_dir (slug + raw-name hash), not the bare
    # slug, so distinct families can't collide onto one file (see I1).
    expected = tmp_path / "crawlers" / family_dir(FAMILY) / "v1.py"
    assert expected.exists()
    assert expected.read_text() == BOOKS_SOURCE


def test_add_version_increments_n_per_family(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    assert registry.add_version(FAMILY, BOOKS_SOURCE) == 1
    assert registry.add_version(FAMILY, BOOKS_SOURCE) == 2
    assert registry.add_version(FAMILY, BOOKS_SOURCE) == 3


def test_add_version_records_status_path_and_sha(registry, tmp_path):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    n = registry.add_version(FAMILY, BOOKS_SOURCE, now="2026-06-13T00:00:00+00:00")
    ladder = registry.version_ladder(FAMILY)
    assert len(ladder) == 1
    v = ladder[0]
    assert v["n"] == n
    # New versions land as 'fallback' — NOT active until set_active is called.
    assert v["status"] == "fallback"
    assert v["path"] == str(tmp_path / "crawlers" / family_dir(FAMILY) / "v1.py")
    assert v["runs"] == 0
    assert v["successes"] == 0
    # source_sha is sha256 of the exact source string.
    expected_sha = hashlib.sha256(BOOKS_SOURCE.encode()).hexdigest()
    row = registry._versions_row(FAMILY, n)
    assert row["source_sha"] == expected_sha


def test_add_version_auto_creates_minimal_family_when_absent(registry):
    # Documented behavior: add_version on an unknown family auto-creates a minimal
    # family row rather than failing, so the Loop can bootstrap a brand-new family.
    n = registry.add_version("new.example.com/list", BOOKS_SOURCE)
    assert n == 1
    fam = registry.get_family("new.example.com/list")
    assert fam is not None
    assert fam["family"] == "new.example.com/list"


# --------------------------------------------------------------------------- #
# set_active — flip active pointer, demote previous active to fallback
# --------------------------------------------------------------------------- #


def test_set_active_marks_version_active(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    assert registry._versions_row(FAMILY, n)["status"] == "active"


def test_set_active_demotes_previous_active_to_fallback(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, v1)
    registry.set_active(FAMILY, v2)
    assert registry._versions_row(FAMILY, v1)["status"] == "fallback"
    assert registry._versions_row(FAMILY, v2)["status"] == "active"


def test_set_active_leaves_archived_versions_untouched(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v3 = registry.add_version(FAMILY, BOOKS_SOURCE)
    # Archive v1 directly, then flip active between v2 and v3.
    registry._set_status(FAMILY, v1, "archived")
    registry.set_active(FAMILY, v2)
    registry.set_active(FAMILY, v3)
    # v1 must remain archived (set_active only demotes the *active* one).
    assert registry._versions_row(FAMILY, v1)["status"] == "archived"
    assert registry._versions_row(FAMILY, v2)["status"] == "fallback"
    assert registry._versions_row(FAMILY, v3)["status"] == "active"


def test_set_active_to_nonexistent_version_raises_and_keeps_current_active(registry):
    # I2: promoting a version that does not exist must NOT demote the current
    # active one — otherwise the family would be left with zero active versions.
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, v1)
    with pytest.raises(LookupError):
        registry.set_active(FAMILY, 999)  # no such version
    # The previously-active version is STILL active; nothing was demoted.
    assert registry._versions_row(FAMILY, v1)["status"] == "active"


# --------------------------------------------------------------------------- #
# version_ladder — active first, then remaining by n desc
# --------------------------------------------------------------------------- #


def test_version_ladder_empty_for_unknown_family(registry):
    assert registry.version_ladder("nope.example.com/x") == []


def test_version_ladder_orders_active_first_then_n_desc(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v3 = registry.add_version(FAMILY, BOOKS_SOURCE)
    # Make v2 the active one (NOT the highest n) to prove "active first" ordering.
    registry.set_active(FAMILY, v2)
    ladder = registry.version_ladder(FAMILY)
    ns = [v["n"] for v in ladder]
    # Active v2 first, then the rest (v3, v1) by n desc.
    assert ns == [v2, v3, v1]
    assert ladder[0]["status"] == "active"


def test_version_ladder_dicts_have_expected_keys(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    registry.add_version(FAMILY, BOOKS_SOURCE)
    (entry,) = registry.version_ladder(FAMILY)
    assert set(entry) == {"n", "status", "path", "runs", "successes"}


# --------------------------------------------------------------------------- #
# record_run — increment runs (and successes when ok)
# --------------------------------------------------------------------------- #


def test_record_run_increments_runs_and_successes(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.record_run(FAMILY, n, ok=True)
    registry.record_run(FAMILY, n, ok=False)
    registry.record_run(FAMILY, n, ok=True)
    row = registry._versions_row(FAMILY, n)
    assert row["runs"] == 3
    assert row["successes"] == 2


def test_record_run_on_unknown_version_raises(registry):
    # M2: recording a run against a (family, n) that does not exist must not
    # silently no-op — it signals a logic error and so raises LookupError.
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    registry.add_version(FAMILY, BOOKS_SOURCE)  # v1 exists
    with pytest.raises(LookupError):
        registry.record_run(FAMILY, 999, ok=True)  # no v999


# --------------------------------------------------------------------------- #
# rollback — demote current active, promote most-recent non-archived fallback
# --------------------------------------------------------------------------- #


def test_rollback_promotes_most_recent_fallback(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v3 = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, v3)  # v3 active; v1, v2 fallback
    new_active = registry.rollback(FAMILY)
    # The bad active (v3) is demoted, and the most recent fallback (v2) is promoted.
    assert new_active == v2
    assert registry._versions_row(FAMILY, v2)["status"] == "active"
    # The rolled-back version must no longer be active.
    assert registry._versions_row(FAMILY, v3)["status"] != "active"
    # The older fallback (v1) is not chosen and stays a fallback.
    assert registry._versions_row(FAMILY, v1)["status"] == "fallback"


def test_rollback_skips_archived_versions(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v3 = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry._set_status(FAMILY, v2, "archived")  # v2 archived: not a rollback target
    registry.set_active(FAMILY, v3)
    new_active = registry.rollback(FAMILY)
    # v2 is archived, so rollback skips it and lands on v1.
    assert new_active == v1
    assert registry._versions_row(FAMILY, v1)["status"] == "active"


def test_rollback_raises_when_no_fallback_available(registry):
    registry.upsert_family(FAMILY, ["^x$"], "Product@1")
    only = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, only)
    # No other non-archived version to promote.
    with pytest.raises(Exception):
        registry.rollback(FAMILY)


# --------------------------------------------------------------------------- #
# run_history — record_history writer + recent_history reader (M6 executor)
# --------------------------------------------------------------------------- #

URL = "http://books.toscrape.com/catalogue/page-1.html"


def test_record_history_roundtrips_items(registry):
    items = [
        {"name": "A Light in the Attic", "price": "51.77"},
        {"name": "Tipping the Velvet", "price": "53.74"},
    ]
    registry.record_history(FAMILY, URL, 1, items)
    out = registry.recent_history(FAMILY, URL)
    assert len(out) == 1
    row = out[0]
    assert row["family"] == FAMILY
    assert row["url"] == URL
    assert row["version"] == 1
    # extracted_json is parsed back into the original list of dicts.
    assert row["items"] == items


def test_recent_history_is_newest_first(registry):
    registry.record_history(FAMILY, URL, 1, [{"name": "first"}], now="2026-06-13T00:00:01+00:00")
    registry.record_history(FAMILY, URL, 1, [{"name": "second"}], now="2026-06-13T00:00:02+00:00")
    out = registry.recent_history(FAMILY, URL)
    # Newest row (the second insert) comes first.
    assert [r["items"][0]["name"] for r in out] == ["second", "first"]


def test_recent_history_filters_by_url_when_given(registry):
    other_url = "http://books.toscrape.com/catalogue/page-2.html"
    registry.record_history(FAMILY, URL, 1, [{"name": "p1"}])
    registry.record_history(FAMILY, other_url, 1, [{"name": "p2"}])
    only_p1 = registry.recent_history(FAMILY, URL)
    assert [r["url"] for r in only_p1] == [URL]
    # With no url filter, both rows come back (still newest-first).
    both = registry.recent_history(FAMILY)
    assert {r["url"] for r in both} == {URL, other_url}


def test_recent_history_honors_limit(registry):
    for i in range(5):
        registry.record_history(
            FAMILY, URL, 1, [{"i": i}], now=f"2026-06-13T00:00:0{i}+00:00"
        )
    out = registry.recent_history(FAMILY, URL, limit=2)
    assert len(out) == 2
    # The two most recent (i=4, i=3).
    assert [r["items"][0]["i"] for r in out] == [4, 3]


def test_record_history_serializes_non_json_native_values(registry):
    # The executor stores coerced values like Decimal prices; record_history must
    # JSON-encode them (default=str) rather than crash.
    from decimal import Decimal

    registry.record_history(FAMILY, URL, 1, [{"price": Decimal("51.77")}])
    out = registry.recent_history(FAMILY, URL)
    # Decimal was stringified on the way in; it round-trips as a string.
    assert out[0]["items"][0]["price"] == "51.77"


# --------------------------------------------------------------------------- #
# active_source — the SOURCE string of a family's active version (M9 Loop helper)
# --------------------------------------------------------------------------- #


def test_active_source_returns_active_version_source(registry):
    """active_source returns the exact source string of the family's ACTIVE
    version — the Loop feeds it to codegen as prev_source so a regeneration can
    build on the version it is replacing."""
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    assert registry.active_source(FAMILY) == BOOKS_SOURCE


def test_active_source_none_when_no_active_version(registry):
    """A family with a registered-but-not-active version (or none at all) has no
    active source -> None, so a brand-new family bootstraps with prev_source
    None down the same code path."""
    # No versions at all.
    assert registry.active_source(FAMILY) is None
    # A version exists but was never promoted to active.
    registry.add_version(FAMILY, BOOKS_SOURCE)
    assert registry.active_source(FAMILY) is None


def test_active_source_tracks_the_active_rung(registry):
    """active_source follows set_active: after promoting v2 it returns v2's
    source, not v1's."""
    v1_src = _marked_source("V::1")
    v2_src = _marked_source("V::2")
    n1 = registry.add_version(FAMILY, v1_src)
    registry.set_active(FAMILY, n1)
    assert registry.active_source(FAMILY) == v1_src
    n2 = registry.add_version(FAMILY, v2_src)
    registry.set_active(FAMILY, n2)
    assert registry.active_source(FAMILY) == v2_src


# --------------------------------------------------------------------------- #
# set_family_status — update the families.status column (M9 escalation)
# --------------------------------------------------------------------------- #


def test_set_family_status_updates_status(registry):
    """set_family_status updates the family's status in place (the Loop marks a
    family 'escalated' when regeneration gives up)."""
    registry.upsert_family(FAMILY, [], "Product@1")
    assert registry.get_family(FAMILY)["status"] == "healthy"
    registry.set_family_status(FAMILY, "escalated")
    assert registry.get_family(FAMILY)["status"] == "escalated"


def test_set_family_status_unknown_family_raises(registry):
    """Setting status on a family with no row is a logic error, not a silent
    no-op (the families row must exist — add_version auto-creates it)."""
    with pytest.raises(LookupError):
        registry.set_family_status("nope.example.com/x", "escalated")


# --------------------------------------------------------------------------- #
# active_residual_fields — the hybrid's per-version residual set (Wave 3)
# --------------------------------------------------------------------------- #


def test_active_residual_fields_default_empty_for_unknown_family(registry):
    """An unknown family has no residual set -> [] (the $0 / no-LLM default)."""
    assert registry.active_residual_fields(FAMILY) == []


def test_active_residual_fields_default_empty_when_no_active_version(registry):
    """A family with a version but no active one yet -> [] (nothing promoted)."""
    registry.add_version(FAMILY, BOOKS_SOURCE)
    assert registry.active_residual_fields(FAMILY) == []


def test_active_residual_fields_default_empty_for_legacy_promote(registry):
    """A promote audit written WITHOUT residual_fields (legacy) reads back as [].

    Back-compat: versions promoted before the hybrid existed have no residual_fields
    key in their promote audit data, so the getter must default to [] rather than
    raise — a legacy active version simply runs deterministic-only.
    """
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    registry.write_audit("promote", family=FAMILY, data={"to_version": n})
    assert registry.active_residual_fields(FAMILY) == []


def test_active_residual_fields_reads_persisted_set(registry):
    """The residual set persisted in the active version's promote audit is read back."""
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    registry.write_audit(
        "promote",
        family=FAMILY,
        data={"to_version": n, "residual_fields": ["full_address", "property_type"]},
    )
    assert registry.active_residual_fields(FAMILY) == [
        "full_address",
        "property_type",
    ]


def test_active_residual_fields_tracks_the_active_version(registry):
    """The getter follows the ACTIVE rung: promoting v2 swaps in v2's residual set.

    Each version's residual set lives in its own promote audit (keyed by to_version);
    the getter resolves the currently-active version first, so a rollback/repromote
    serves the residual set of whatever version is active now — not the newest ever.
    """
    n1 = registry.add_version(FAMILY, _marked_source("V::1"))
    registry.set_active(FAMILY, n1)
    registry.write_audit(
        "promote", family=FAMILY, data={"to_version": n1, "residual_fields": ["a"]}
    )
    n2 = registry.add_version(FAMILY, _marked_source("V::2"))
    registry.set_active(FAMILY, n2)
    registry.write_audit(
        "promote", family=FAMILY, data={"to_version": n2, "residual_fields": ["b", "c"]}
    )
    # Active is v2 -> v2's set.
    assert registry.active_residual_fields(FAMILY) == ["b", "c"]
    # Roll the active pointer back to v1 -> v1's set (its own promote audit).
    registry.set_active(FAMILY, n1)
    assert registry.active_residual_fields(FAMILY) == ["a"]
