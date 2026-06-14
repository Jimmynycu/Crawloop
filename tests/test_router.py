"""Tests for the URL-regex router (Task 10.1): :func:`route`.

The router is the engine's first decision: given a URL, which registered family
(if any) owns it? A family stores ``url_patterns`` as a JSON list of regex
strings; :func:`route` returns the family whose patterns match, iterating
families in a deterministic order (by family name) so the first match is stable.
A malformed pattern in one family's list is skipped rather than crashing the
whole route — one bad row must never take down routing for every other family.

These run against a real in-memory :class:`Registry` (no network, no LLM): the
router is a pure read over stored patterns.
"""

from __future__ import annotations

import pytest

from crawloop.registry import Registry
from crawloop.router import route

SCHEMA = "Product@1"
# Matches the fixture-server listing routes on the loopback host.
LISTING_PATTERN = r"^https?://127\.0\.0\.1.*/catalogue/.*"


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


# --------------------------------------------------------------------------- #
# Registry.all_families() helper (TDD'd here alongside the router that needs it)
# --------------------------------------------------------------------------- #


def test_all_families_empty_registry(registry):
    """A fresh registry has no families."""
    assert registry.all_families() == []


def test_all_families_returns_rows_sorted_by_name(registry):
    """Every family row is returned, sorted by family name for determinism, with
    ``url_patterns`` decoded back into a list (the same shape as get_family)."""
    registry.upsert_family("b.example/list", [r"^https?://b\."], SCHEMA)
    registry.upsert_family("a.example/list", [r"^https?://a\.", r"^https?://a2\."], SCHEMA)

    families = registry.all_families()
    assert [f["family"] for f in families] == ["a.example/list", "b.example/list"]
    assert families[0]["url_patterns"] == [r"^https?://a\.", r"^https?://a2\."]
    assert families[0]["schema_ref"] == SCHEMA


# --------------------------------------------------------------------------- #
# route()
# --------------------------------------------------------------------------- #


def test_route_matches_family_pattern(registry):
    """A listing URL matches the family whose stored regex covers it."""
    registry.upsert_family("127.0.0.1/product_list", [LISTING_PATTERN], SCHEMA)
    url = "http://127.0.0.1:8080/catalogue/page-1.html"
    assert route(url, registry) == "127.0.0.1/product_list"


def test_route_unmatched_url_returns_none(registry):
    """A URL no family's patterns match routes to None."""
    registry.upsert_family("127.0.0.1/product_list", [LISTING_PATTERN], SCHEMA)
    assert route("http://127.0.0.1:8080/other/thing.html", registry) is None


def test_route_empty_registry_returns_none(registry):
    """Nothing registered -> nothing matches."""
    assert route("http://127.0.0.1:8080/catalogue/x.html", registry) is None


def test_route_skips_family_with_no_patterns(registry):
    """A family with an empty pattern list never matches (and does not crash)."""
    registry.upsert_family("127.0.0.1/empty", [], SCHEMA)
    assert route("http://127.0.0.1:8080/catalogue/x.html", registry) is None


def test_route_skips_malformed_pattern_without_crashing(registry):
    """A family whose pattern is an invalid regex is skipped; a later well-formed
    family still matches. The bad pattern must not raise out of route()."""
    # 'bad' sorts before 'good' by family name, so the malformed one is tried
    # first — proving a bad pattern early in iteration does not abort the walk.
    registry.upsert_family("bad", ["([unclosed", r"also[bad"], SCHEMA)
    registry.upsert_family("good", [LISTING_PATTERN], SCHEMA)
    url = "http://127.0.0.1:8080/catalogue/page-1.html"
    assert route(url, registry) == "good"


def test_route_first_match_wins_by_family_name_order(registry):
    """When two families both match a URL, the earlier family-name wins
    (documented deterministic ordering)."""
    shared = r"^https?://127\.0\.0\.1.*"
    registry.upsert_family("zeta/list", [shared], SCHEMA)
    registry.upsert_family("alpha/list", [shared], SCHEMA)
    assert route("http://127.0.0.1:8080/catalogue/x", registry) == "alpha/list"


def test_route_search_not_fullmatch(registry):
    """Patterns are applied with re.search semantics (an unanchored substring
    pattern matches anywhere in the URL), so operators can write loose hooks."""
    registry.upsert_family("127.0.0.1/product_list", ["/catalogue/"], SCHEMA)
    assert route("http://127.0.0.1:8080/catalogue/page-1.html", registry) == (
        "127.0.0.1/product_list"
    )
