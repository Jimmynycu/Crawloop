"""Tests for the full extraction Loop driver (Task 9.5): :func:`run_loop`.

This is the keystone of M9: the driver wires the whole regeneration loop together
— SAMPLE (9.1) -> ORACLE (T2 direct_extract) -> CODEGEN (9.2) -> GAUNTLET (9.4)
-> PROMOTE (9.5), with a bounded round retry that carries a failure report, and
escalation when there are no samples, no usable oracle, or the rounds are
exhausted. New-family bootstrap is the same code path with ``prev_source=None``.

Real components throughout: the :class:`FixtureServer` (normal mode) behind a
:class:`RealFetchContext` authorizing only ``127.0.0.1`` does the real fetching;
the real subprocess sandbox runs candidates; the real validator scores them; a
real in-memory :class:`Registry` records versions/audit. The ONLY fake is the
:class:`FakeCompleter`, scripted with exactly the model responses each path needs
(the oracle's JSON per sample, then the candidate source per round) — so NO real
model and NO network beyond localhost. ``n_samples=1`` + ``k=1`` keep the script
small and the subprocess usage modest so the suite stays fast.
"""

from __future__ import annotations

import json

import httpx

from crawloop.access import RealFetchContext
from crawloop.config import AppConfig, DomainConfig
from crawloop.llm import FakeCompleter
from crawloop.loop.driver import LoopResult, run_loop
from crawloop.loop.promote import load_fixtures
from crawloop.registry import Registry, family_dir

FAMILY = "127.0.0.1/product_list"
SCHEMA = "Product@1"


# --------------------------------------------------------------------------- #
# Test doubles for the fetch context (mirror tests/test_sampler.py).
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


class InMemoryAccessStore:
    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


def _local_config() -> AppConfig:
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=100.0,
        render_js=False,
        access_strategies=[("plain", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _ctx(client: httpx.AsyncClient) -> RealFetchContext:
    return RealFetchContext(
        _local_config(),
        InMemoryAccessStore(),
        client=client,
        browser_runner=FakeBrowserRunner(),
    )


# --------------------------------------------------------------------------- #
# Candidate sources (gate-clean books crawlers, as a model would emit them).
# --------------------------------------------------------------------------- #


def _fenced(source: str) -> str:
    return f"```python\n{source}```"


# A CORRECT books crawler matching the fixture server's `normal` layout.
_CORRECT = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksList(Crawler):
    family = "127.0.0.1/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            avail = ctx.clean_text(" ".join(card.css(".availability::text").getall())) or ""
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in avail,
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        return CrawlResult(items=items, next_url=None)
'''

# A WRONG-ELEMENT crawler: reads availability text into `name`. Schema-valid but
# disagrees with the oracle on every item's name -> agreement under the bar.
_WRONG = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksWrong(Crawler):
    family = "127.0.0.1/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            avail = ctx.clean_text(" ".join(card.css(".availability::text").getall())) or ""
            items.append({
                "name": ctx.clean_text(card.css(".availability::text").get()),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in avail,
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        return CrawlResult(items=items, next_url=None)
'''


def _oracle_for_root(base: str) -> str:
    """The oracle's JSON array for the fixture server's root listing page.

    The root page (``/``) serves the three page-1 books; a crawler running at
    ``base + "/"`` absolutizes the relative ``catalogue/...`` hrefs against the
    root, so the oracle URLs are ``base/catalogue/<slug>/index.html``. Returned
    as a JSON string (what the model would emit) so direct_extract parses +
    validates it as a real oracle extraction.
    """
    records = [
        {
            "name": "A Light in the Attic",
            "price": "51.77",
            "in_stock": True,
            "url": f"{base}/catalogue/a-light-in-the-attic/index.html",
        },
        {
            "name": "Tipping the Velvet",
            "price": "53.74",
            "in_stock": True,
            "url": f"{base}/catalogue/tipping-the-velvet/index.html",
        },
        {
            "name": "Soumission",
            "price": "50.10",
            "in_stock": False,
            "url": f"{base}/catalogue/soumission/index.html",
        },
    ]
    return json.dumps(records)


def _oracle_for_page1(base: str) -> str:
    """Oracle for the ``/catalogue/page-1.html`` route (same 3 books as ``/``).

    Served under ``/catalogue/``, so the relative ``catalogue/<slug>/`` hrefs
    absolutize to the DOUBLED ``/catalogue/catalogue/<slug>/index.html`` — exactly
    what `_CORRECT` extracts there. (The route serves identical book content to
    ``/``; only the base URL, and therefore the absolutized urls, differ.)
    """
    records = [
        {"name": "A Light in the Attic", "price": "51.77", "in_stock": True,
         "url": f"{base}/catalogue/catalogue/a-light-in-the-attic/index.html"},
        {"name": "Tipping the Velvet", "price": "53.74", "in_stock": True,
         "url": f"{base}/catalogue/catalogue/tipping-the-velvet/index.html"},
        {"name": "Soumission", "price": "50.10", "in_stock": False,
         "url": f"{base}/catalogue/catalogue/soumission/index.html"},
    ]
    return json.dumps(records)


def _oracle_for_page2(base: str) -> str:
    """Oracle for the ``/catalogue/page-2.html`` route (the 4th book).

    Same doubled-``catalogue`` absolutization as page 1, for the single page-2
    book Sharp Objects — matching `_CORRECT`'s extraction there.
    """
    records = [
        {"name": "Sharp Objects", "price": "47.82", "in_stock": True,
         "url": f"{base}/catalogue/catalogue/sharp-objects/index.html"},
    ]
    return json.dumps(records)


# --------------------------------------------------------------------------- #
# Happy path: sample -> oracle -> codegen -> gauntlet -> promote (the keystone)
# --------------------------------------------------------------------------- #


async def test_run_loop_happy_promote(fixture_server, tmp_path):
    """A correct candidate clears the gauntlet -> the driver promotes it:
    LoopResult.ok, a new ACTIVE version on the ladder, a "promote" audit entry,
    and the samples + oracles persisted as fixtures under the family's dir.

    The promote floor requires >= 3 usable oracles (design §2/§9/§15), so this
    uses n_samples=3. The fixture server's ``/`` only harvests 2 reachable pages
    (the book detail links 404), so three explicit listing-route seeds are given
    — ``/``, ``/catalogue/page-1.html``, ``/catalogue/page-2.html`` — which
    collect_samples returns in that order. k=1 -> the FakeCompleter script is the
    three per-page oracles in sample order, then the one correct candidate.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    seeds = [f"{base}/", f"{base}/catalogue/page-1.html", f"{base}/catalogue/page-2.html"]
    completer = FakeCompleter([
        _oracle_for_root(base),   # sample 0: "/"
        _oracle_for_page1(base),  # sample 1: "/catalogue/page-1.html"
        _oracle_for_page2(base),  # sample 2: "/catalogue/page-2.html"
        _fenced(_CORRECT),        # round 1 candidate
    ])

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, seeds, ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
            now="2026-06-13T00:00:00+00:00",
        )

    assert isinstance(result, LoopResult)
    assert result.ok is True
    assert result.escalated is False
    assert result.version is not None
    assert result.rounds == 1
    assert result.reason == "promoted"

    # The promoted version is the family's ACTIVE rung.
    ladder = registry.version_ladder(FAMILY)
    active = [v for v in ladder if v["status"] == "active"]
    assert len(active) == 1
    assert active[0]["n"] == result.version

    # The active source loads + is the correct crawler (round-trips through the
    # gated loader, proving promote registered real, runnable source).
    crawler = registry.load_crawler(FAMILY)
    assert crawler.family == FAMILY

    # A promote audit entry was written for this family.
    promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
    assert len(promotes) == 1
    assert promotes[0]["data"]["to_version"] == result.version
    assert promotes[0]["data"]["schema_ref"] == SCHEMA

    # The hybrid residual set was computed + persisted at promote. The _CORRECT
    # books crawler fills every Product@1 field the oracle has (name/price/
    # in_stock/url), so there is NOTHING left to tail-fill -> [] is persisted and
    # the active version reads back []: this family runs deterministic-only, $0.
    assert promotes[0]["data"]["residual_fields"] == []
    assert registry.active_residual_fields(FAMILY) == []

    # All three samples + oracles were saved as fixtures (one per sample, ordered
    # by filename = sample order). Fixture 0 is the "/" page's three books.
    fixture_dir = fixtures_dir / family_dir(FAMILY)
    assert fixture_dir.is_dir()
    loaded = load_fixtures(fixtures_dir, FAMILY)
    assert len(loaded) == 3
    fx_html, fx_expected = loaded[0]
    assert "product_pod" in fx_html
    assert [r["name"] for r in fx_expected] == [
        "A Light in the Attic",
        "Tipping the Velvet",
        "Soumission",
    ]


# --------------------------------------------------------------------------- #
# Gate 5 (history cross-check) is RUN + AUDITED at promote (I4): a large move in
# a volatile field (price) vs the family's recent history is recorded in the
# promote audit's data["history_warnings"] — non-fatal (promotion still
# succeeds). Empty history -> no warnings (no behavior change).
# --------------------------------------------------------------------------- #


async def test_promote_records_history_warning_on_price_jump(fixture_server, tmp_path):
    """A promote where recent_history shows a much-lower prior price for a book
    the new crawler extracts (>50% jump) records a non-empty
    data["history_warnings"] in the promote audit — and STILL promotes (Gate 5 is
    non-fatal at runtime).

    The promoted crawler runs sample 0 ("/") and yields "A Light in the Attic" at
    51.77 with url {base}/catalogue/a-light-in-the-attic/index.html. Seeding
    recent_history with that same url at a prior price of 5.00 is a >50% jump, so
    history_crosscheck flags it. (history_crosscheck aligns by url and jump-checks
    the volatile `price` field.)
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    # Prior history for this family: same book url, but a much lower price.
    a_light_url = f"{base}/catalogue/a-light-in-the-attic/index.html"
    registry.upsert_family(FAMILY, [r"^https?://127\.0\.0\.1.*"], SCHEMA)
    registry.record_history(
        FAMILY,
        f"{base}/",
        1,
        [{"name": "A Light in the Attic", "price": "5.00",
          "in_stock": True, "url": a_light_url}],
    )

    seeds = [f"{base}/", f"{base}/catalogue/page-1.html", f"{base}/catalogue/page-2.html"]
    completer = FakeCompleter([
        _oracle_for_root(base),
        _oracle_for_page1(base),
        _oracle_for_page2(base),
        _fenced(_CORRECT),
    ])

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, seeds, ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
            now="2026-06-13T00:00:00+00:00",
        )

    # Promotion STILL succeeds (Gate 5 is non-fatal).
    assert result.ok is True
    assert result.reason == "promoted"

    # The promote audit carries a non-empty history_warnings naming the jump.
    promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
    assert len(promotes) == 1
    warnings = promotes[0]["data"]["history_warnings"]
    assert warnings  # non-empty
    assert any("price jumped" in w for w in warnings)
    assert any("5.00" in w and "51.77" in w for w in warnings)


async def test_promote_no_history_warnings_with_empty_history(fixture_server, tmp_path):
    """A first promotion (no prior history) records NO history warnings — Gate 5
    returns [] on empty history, so the promote audit's history_warnings is an
    empty list and promotion is unaffected (backward-compatible)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    seeds = [f"{base}/", f"{base}/catalogue/page-1.html", f"{base}/catalogue/page-2.html"]
    completer = FakeCompleter([
        _oracle_for_root(base),
        _oracle_for_page1(base),
        _oracle_for_page2(base),
        _fenced(_CORRECT),
    ])

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, seeds, ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
            now="2026-06-13T00:00:00+00:00",
        )

    assert result.ok is True
    promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
    assert len(promotes) == 1
    # The key exists and is an empty list (Gate 5 ran, found nothing to warn on).
    assert promotes[0]["data"]["history_warnings"] == []


# --------------------------------------------------------------------------- #
# Escalation: too few usable oracles to bound the LLM-oracle error (design §2/§9)
# --------------------------------------------------------------------------- #


async def test_run_loop_escalates_when_fewer_than_min_oracles(fixture_server, tmp_path):
    """Fewer than ``min_oracles`` (default 3) usable oracles -> the driver
    escalates with an "insufficient oracles" reason and NEVER generates a
    candidate, because >= 3 samples are the design's bound on LLM-oracle error
    (§2/§9/§15) — promoting against 1-2 oracles is exactly the wrong-crawler risk.

    The fixture server's ``/`` harvests only 2 reachable pages, so even asking
    for n_samples=3 yields 2 usable oracles. The script includes a (correct)
    candidate that MUST stay unconsumed: if the floor were not enforced the
    driver would reach codegen and promote, so an unescalated result fails here.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    # Two oracles (one per reachable sample) + a candidate that must NOT be used.
    completer = FakeCompleter(
        [_oracle_for_root(base), _oracle_for_page2(base), _fenced(_CORRECT)]
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, [f"{base}/"], ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
        )

    assert result.ok is False
    assert result.escalated is True
    assert result.version is None
    assert result.rounds == 0  # escalated before any codegen/gauntlet round
    assert "insufficient oracles" in result.reason.lower()
    # Only the two oracle calls happened; the candidate was never requested.
    assert len(completer.calls) == 2
    # No active version, and an escalation was audited + the family marked.
    ladder = registry.version_ladder(FAMILY)
    assert all(v["status"] != "active" for v in ladder)
    events = [e["event"] for e in registry.read_audit(FAMILY)]
    assert "escalated" in events
    assert registry.get_family(FAMILY)["status"] == "escalated"


# --------------------------------------------------------------------------- #
# Escalation: gauntlet never crowns a winner over max_rounds
# --------------------------------------------------------------------------- #


async def test_run_loop_escalates_after_max_rounds(fixture_server, tmp_path):
    """Codegen returns a WRONG-element crawler every round (oracles stay good) ->
    no candidate passes the gauntlet -> after max_rounds the driver escalates:
    LoopResult.escalated, NO new active version, an "escalated" audit entry, and
    the family marked escalated.

    Three seeds clear the >= 3 usable-oracle floor; the oracles are extracted once
    (before the rounds), so for max_rounds=2 the script is the three per-sample
    oracles, then wrong_round1, wrong_round2.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    seeds = [f"{base}/", f"{base}/catalogue/page-1.html", f"{base}/catalogue/page-2.html"]
    completer = FakeCompleter(
        [_oracle_for_root(base), _oracle_for_page1(base), _oracle_for_page2(base),
         _fenced(_WRONG), _fenced(_WRONG)]
    )

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, seeds, ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=3,
        )

    assert result.ok is False
    assert result.escalated is True
    assert result.version is None
    assert result.rounds == 2

    # No active version was created.
    ladder = registry.version_ladder(FAMILY)
    assert all(v["status"] != "active" for v in ladder)
    # An "escalated" audit entry exists and the family is marked escalated.
    events = [e["event"] for e in registry.read_audit(FAMILY)]
    assert "escalated" in events
    assert registry.get_family(FAMILY)["status"] == "escalated"


# --------------------------------------------------------------------------- #
# Escalation: the oracle itself fails on every sample
# --------------------------------------------------------------------------- #


async def test_run_loop_escalates_on_oracle_failure(fixture_server, tmp_path):
    """When direct_extract fails for every sample (malformed JSON beyond the
    repair budget), there is no usable oracle to generate against -> the driver
    escalates with a reason mentioning the oracle, BEFORE any codegen.

    direct_extract does 1 + max_repairs (=1) calls per sample; with n_samples=1
    the script is two malformed responses and NO candidate response.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    completer = FakeCompleter(["not json at all", "still not valid json"])

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, [f"{base}/"], ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=1,
        )

    assert result.ok is False
    assert result.escalated is True
    assert result.version is None
    assert "oracle" in result.reason.lower()
    # No candidate was ever generated (the completer's two responses were both
    # consumed by the oracle's attempt + repair).
    assert len(completer.calls) == 2
    # An escalation was audited.
    events = [e["event"] for e in registry.read_audit(FAMILY)]
    assert "escalated" in events


# --------------------------------------------------------------------------- #
# Escalation: sampling yields nothing
# --------------------------------------------------------------------------- #


async def test_run_loop_escalates_on_no_samples(fixture_server, tmp_path):
    """No reachable pages -> the sampler returns [] -> the driver escalates with
    a "no samples" reason without ever calling the model.

    The single seed 404s (an unknown route) and has no harvestable links, so
    collect_samples returns nothing.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    completer = FakeCompleter([])  # never called

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        result = await run_loop(
            FAMILY, [f"{base}/no-such-page.html"], ctx, registry, completer, SCHEMA,
            fixtures_dir=fixtures_dir, k=1, max_rounds=2, n_samples=1,
        )

    assert result.ok is False
    assert result.escalated is True
    assert result.version is None
    assert "sample" in result.reason.lower()
    assert completer.calls == []  # the model was never reached
