"""Offline proof that the Loop promotes the DETERMINISTIC value-path crawler when
the LLM produces nothing usable (Task 2 wiring).

This is the head-to-head the whole strategy exists for: the model is scripted to
return the per-sample oracle (so the loop has a trusted ground truth) and then
PROSE with no code fence for every codegen round (so LLM codegen yields zero
candidates). The sample pages each embed the complete record as a Next.js
``__NEXT_DATA__`` JSON island whose values match the oracle — with the salary stored
in units of ten thousand (so the oracle's full figure is x10000 the leaf). With no
LLM candidate to lean on, ``run_loop`` must discover the value->paths, emit a
crawler, and promote it as version 1 purely deterministically.

Fully offline: a tiny in-memory :class:`_BlobContext` serves fixed HTML per URL
(no network), an in-memory :class:`Registry`, the real subprocess sandbox (offline
by construction), the real validator/gauntlet/promote, and a scripted
:class:`FakeCompleter` (no model, no network).
"""

from __future__ import annotations

import json

from crawloop.llm import FakeCompleter
from crawloop.loop.driver import run_loop
from crawloop.loop.promote import load_fixtures
from crawloop.registry import Registry, family_dir

FAMILY = "jobs.example.com/posting"
SCHEMA = "JobPosting@1"

# Three detail pages. Each carries a __NEXT_DATA__ island holding the full record;
# salaryK is in units of 10k (oracle salary is x10000). title/company are stored
# verbatim and are value-discoverable; they line up across all three samples at the
# SAME path, so discover_paths keeps them.
_PAGES = {
    "https://jobs.example.com/posting/A": {
        "salaryK": 12, "title": "Senior Engineer", "company": "Acme Corp",
    },
    "https://jobs.example.com/posting/B": {
        "salaryK": 9, "title": "Data Analyst", "company": "Globex",
    },
    "https://jobs.example.com/posting/C": {
        "salaryK": 7, "title": "Support Lead", "company": "Initech",
    },
}


def _page_html(detail: dict) -> str:
    """Wrap a detail dict as a realistic page with a __NEXT_DATA__ JSON island."""
    blob = json.dumps({"props": {"pageProps": {"detail": detail}}}, ensure_ascii=False)
    return (
        "<!doctype html><html><head><title>job</title></head><body>"
        "<h1>posting</h1>"
        f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'
        "</body></html>"
    )


def _oracle_for(detail: dict) -> str:
    """The oracle JSON array for one page: salary as the full figure (= salaryK x
    10000), plus the two verbatim strings. A single record (a detail page = one
    posting). Returned as the JSON string the model would emit, so direct_extract
    parses + validates it as a real oracle extraction against JobPosting@1."""
    record = {
        "salary": detail["salaryK"] * 10000,
        "title": detail["title"],
        "company": detail["company"],
    }
    return json.dumps([record], ensure_ascii=False)


class _BlobContext:
    """A minimal offline FetchContext: returns the fixed HTML registered for each
    URL and provides the same coercion helpers production uses. No network, no
    rendering — collect_samples only ever calls ``fetch`` here."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages

    async def fetch(self, url: str) -> str:
        return self._pages[url]

    async def fetch_rendered(self, url: str, wait_for: str | None = None) -> str:
        return self._pages[url]

    def absolutize(self, base: str, href):
        from crawloop.contract import absolutize

        return absolutize(base, href)

    def parse_money(self, raw):
        from crawloop.contract import parse_money

        return parse_money(raw)

    def clean_text(self, raw):
        from crawloop.contract import clean_text

        return clean_text(raw)


async def test_run_loop_promotes_value_path_crawler_without_llm(tmp_path):
    """No usable LLM candidate, but the samples embed a JSON blob whose values
    match the oracle -> run_loop discovers the paths, emits a crawler, and PROMOTES
    it as version 1. Proves the deterministic JSON-path strategy stands alone."""
    pages_html = {url: _page_html(detail) for url, detail in _PAGES.items()}
    ctx = _BlobContext(pages_html)
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    seeds = list(_PAGES)
    # Script, in call order: the three per-sample oracles (one each, in seed order),
    # then the path-map proposal call (the driver always attempts it when a JSON
    # island is present), then codegen rounds that produce PROSE with no code fence
    # -> zero LLM candidates. The proposal reply here is also prose, so it is NOT a
    # parseable map and `_path_map_candidate` returns None — leaving the VALUE-PATH
    # candidate to promote on its own (which is what this test proves). k=1,
    # max_rounds=2 -> at most 2 codegen calls.
    completer = FakeCompleter(
        [
            _oracle_for(_PAGES[seeds[0]]),
            _oracle_for(_PAGES[seeds[1]]),
            _oracle_for(_PAGES[seeds[2]]),
            "I cannot map this page.",              # path-map proposal: not JSON
            "Sorry, I cannot write this crawler.",  # round 1 codegen: no fence
            "Still unable to produce code.",        # round 2 (unused if r1 promotes)
        ]
    )

    result = await run_loop(
        FAMILY, seeds, ctx, registry, completer, SCHEMA,
        fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
        now="2026-06-14T00:00:00+00:00",
    )

    # Promoted on round 1 by the deterministic candidate (the LLM gave nothing).
    assert result.ok is True
    assert result.escalated is False
    assert result.version == 1
    assert result.rounds == 1
    assert result.reason == "promoted"

    # The promoted version is the family's ACTIVE rung and loads as a real crawler.
    ladder = registry.version_ladder(FAMILY)
    active = [v for v in ladder if v["status"] == "active"]
    assert len(active) == 1
    assert active[0]["n"] == 1
    crawler = registry.load_crawler(FAMILY)
    assert crawler.family == FAMILY
    assert crawler.schema_ref == SCHEMA

    # The promoted SOURCE is the deterministic value-path crawler (not LLM code):
    # it carries the auto-generated banner and reads json, never parsel.
    source = registry.active_source(FAMILY)
    assert "Auto-generated value-path crawler" in source
    assert "import json" in source
    assert "parsel" not in source

    # Calls consumed before promotion: three oracles, the path-map proposal (which
    # returned prose, not a map, so that candidate was skipped), and the round-1 LLM
    # codegen (which produced nothing usable). The value-path candidate makes NO LLM
    # call — it is the deterministic winner.
    assert len(completer.calls) == 5  # 3 oracles + 1 path-map proposal + 1 codegen

    # Fixtures were saved for all three samples (so a future round can regress).
    fixture_dir = fixtures_dir / family_dir(FAMILY)
    assert fixture_dir.is_dir()
    assert len(load_fixtures(fixtures_dir, FAMILY)) == 3

    # The promote audit records the new version + schema.
    promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
    assert len(promotes) == 1
    assert promotes[0]["data"]["to_version"] == 1
    assert promotes[0]["data"]["schema_ref"] == SCHEMA
