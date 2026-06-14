"""Offline proof that the Loop promotes the PATH-MAP crawler for NORMALIZED fields.

This is the scenario the path-map strategy exists for, and which the verbatim
value->path matcher (:mod:`crawloop.loop.jsonpath`) cannot solve alone. Each
sample page embeds the full record as a Next.js ``__NEXT_DATA__`` island, but the
record fields are NORMALIZED away from what the JSON literally stores:

* ``salary`` is the figure x10000 (the JSON stores units of 10k: ``12`` -> ``120000``),
* ``employment_type`` is an enum code (``"full_time"``) the JSON never spells out — it
  stores the source label (``"Full-time"``),
* ``location`` is city + ", " + region CONCATENATED (no single JSON leaf holds it).

None of those appear verbatim in the JSON, so value->path discovery finds nothing
for them. The model is scripted to (1) return the trusted per-sample oracle, then
(2) propose ONE declarative field map, then (3) emit only PROSE for codegen (zero
LLM code candidates). With no LLM crawler to lean on, ``run_loop`` must propose the
map, emit a deterministic path-map crawler, and promote it as version 1.

Fully offline: a tiny in-memory :class:`_BlobContext` serves fixed HTML per URL, an
in-memory :class:`Registry`, the real subprocess sandbox (offline by construction),
the real validator / gauntlet / promote, and a scripted :class:`FakeCompleter`.
"""

from __future__ import annotations

import json

from crawloop.llm import FakeCompleter
from crawloop.loop.driver import run_loop
from crawloop.loop.promote import load_fixtures
from crawloop.registry import Registry, family_dir

FAMILY = "jobs.example.com/posting"
SCHEMA = "JobPosting@1"

# The base path each page's record lives at inside the __NEXT_DATA__ blob.
_BASE = ["props", "pageProps", "detail"]

# Three detail pages. salaryK is units of 10k (x10000 -> salary); etype is the
# source label (mapped to an enum code); city/region concatenate into location.
# All three line up at the SAME paths across samples.
_PAGES = {
    "https://jobs.example.com/posting/A": {
        "salaryK": 12, "etype": "Full-time",
        "title": "Senior Engineer", "company": "Acme Corp",
        "city": "Springfield", "region": "IL",
    },
    "https://jobs.example.com/posting/B": {
        "salaryK": 9, "etype": "Part-time",
        "title": "Data Analyst", "company": "Globex",
        "city": "Ogdenville", "region": "IL",
    },
    "https://jobs.example.com/posting/C": {
        "salaryK": 7, "etype": "Contract",
        "title": "Support Lead", "company": "Initech",
        "city": "Capital City", "region": "IL",
    },
}

# The enum mapping the model's field map carries (source label -> schema code).
_ETYPE_MAP = {"Full-time": "full_time", "Part-time": "part_time", "Contract": "contract"}


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
    """The oracle JSON array for one page: NORMALIZED values (salary as the full
    figure = salaryK x 10000, employment_type as the enum code, location
    concatenated). Returned as the JSON string the model would emit so direct_extract
    parses + validates it against JobPosting@1 as a real oracle extraction."""
    record = {
        "salary": detail["salaryK"] * 10000,
        "employment_type": _ETYPE_MAP[detail["etype"]],
        "title": detail["title"],
        "company": detail["company"],
        "location": detail["city"] + ", " + detail["region"],
    }
    return json.dumps([record], ensure_ascii=False)


def _fieldmap_json() -> str:
    """The declarative field map the model proposes (Task 2 step): a x10000 salary, an
    enum {map} from the source label, a {concat} location, and two verbatim strings.
    Reproduces every oracle field from each sample's own JSON."""
    fieldmap = {
        "salary": {"path": [*_BASE, "salaryK"], "transform": "x10000"},
        "employment_type": {"path": [*_BASE, "etype"], "transform": {"map": _ETYPE_MAP}},
        "title": {"path": [*_BASE, "title"], "transform": "none"},
        "company": {"path": [*_BASE, "company"], "transform": "none"},
        "location": {
            "path": [],
            "transform": {
                "concat": [[*_BASE, "city"], [*_BASE, "region"]],
                "sep": ", ",
            },
        },
    }
    return json.dumps(fieldmap, ensure_ascii=False)


class _BlobContext:
    """A minimal offline FetchContext: returns the fixed HTML registered for each
    URL and provides the same coercion helpers production uses. No network."""

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


async def test_run_loop_promotes_pathmap_crawler_for_normalized_fields(tmp_path):
    """Normalized fields (x10000 salary, enum-from-label, concat location) live in
    no JSON leaf verbatim, and the LLM gives no code — so run_loop must use the
    proposed field map, emit a path-map crawler, and PROMOTE it as version 1."""
    pages_html = {url: _page_html(detail) for url, detail in _PAGES.items()}
    ctx = _BlobContext(pages_html)
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    seeds = list(_PAGES)
    # Script, in call order: the three per-sample oracles (one each), then the ONE
    # path-map proposal, then codegen rounds that produce PROSE (no code fence) ->
    # zero LLM candidates. k=1, max_rounds=2 -> at most 2 codegen calls.
    completer = FakeCompleter(
        [
            _oracle_for(_PAGES[seeds[0]]),
            _oracle_for(_PAGES[seeds[1]]),
            _oracle_for(_PAGES[seeds[2]]),
            _fieldmap_json(),                       # path-map proposal (1 call)
            "Sorry, I cannot write this crawler.",  # round 1 codegen: no fence
            "Still unable to produce code.",        # round 2 (unused if r1 promotes)
        ]
    )

    result = await run_loop(
        FAMILY, seeds, ctx, registry, completer, SCHEMA,
        fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
        now="2026-06-14T00:00:00+00:00",
    )

    # Promoted on round 1 by the path-map candidate (the LLM gave no code).
    assert result.ok is True
    assert result.escalated is False
    assert result.version == 1
    assert result.rounds == 1
    assert result.reason == "promoted"

    # The promoted version is the family's ACTIVE rung and loads as a real crawler.
    ladder = registry.version_ladder(FAMILY)
    active = [v for v in ladder if v["status"] == "active"]
    assert len(active) == 1 and active[0]["n"] == 1
    crawler = registry.load_crawler(FAMILY)
    assert crawler.family == FAMILY
    assert crawler.schema_ref == SCHEMA

    # The promoted SOURCE is the deterministic PATH-MAP crawler (not LLM code, not
    # the verbatim value-path crawler): it carries the path-map banner and reads
    # json, never parsel.
    source = registry.active_source(FAMILY)
    assert "Auto-generated path-map crawler" in source
    assert "import json" in source
    assert "parsel" not in source

    # Only the three oracle calls + the one map proposal were consumed before the
    # round-1 codegen call that produced nothing usable (4 + 1 = 5).
    assert len(completer.calls) == 5

    # Fixtures were saved for all three samples (so a future round can regress).
    fixture_dir = fixtures_dir / family_dir(FAMILY)
    assert fixture_dir.is_dir()
    assert len(load_fixtures(fixtures_dir, FAMILY)) == 3

    # The promote audit records the new version + schema.
    promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
    assert len(promotes) == 1
    assert promotes[0]["data"]["to_version"] == 1
    assert promotes[0]["data"]["schema_ref"] == SCHEMA
