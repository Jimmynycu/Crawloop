"""FLAGSHIP end-to-end test (Task 11.2): the whole self-heal + access-recovery
cycle, OFFLINE and DETERMINISTIC, through the REAL engine.

This is the product proof. One :class:`~crawloop.engine.Engine`, the real
:class:`FixtureServer`, the real registry / executor / validator / sandbox /
recovery — the ONLY fake is the :class:`~crawloop.llm.FakeCompleter`, scripted
from a committed JSON "cassette" (``tests/cassettes/selfheal.json``). No real
model, no API key, no network beyond localhost.

The cassette is an ORDERED list of completion strings (oracle JSON arrays + a
```python codegen block), replayed positionally. ``{BASE}`` in each string is
replaced with the fixture server's ephemeral ``http://127.0.0.1:<port>`` at load
time (the only thing that can't be frozen, since the port is assigned per run).
The order is exactly the order the engine calls the model during step 2 — see the
cassette's own notes. Six responses total:

1. drift fallback (T2 ``direct_extract`` of the requested page)
2-4. the inline Loop's three per-sample oracles
5-6. the inline Loop's two codegen candidates (k=2)

The four scripted steps, each asserted:

1. **Seed v1 + fast path.** A family whose v1 parses the NORMAL layout serves
   ``source=="registry"`` with the 3 books and the FakeCompleter NOT called.
2. **Break the layout -> fallback serves NOW + promote.** Server -> MUTATED: the
   v1 crawler yields nothing, the engine serves the 3 books via T2 (``fallback``)
   and the inline Loop PROMOTES a v2 that parses the mutated layout (new active
   version, a "promote" audit entry, fixtures written).
3. **Reuse the healed crawler, fast again.** Still MUTATED: ``source=="registry"``,
   ``used_version==2``, correct items, and NO new model calls this request.
4. **Blocked -> access recovery heals fetching.** Server -> BLOCKED: recovery
   applies the ``bypass_token`` strategy, gets through, the engine retries the
   ladder -> ``source=="recovered"``, ``recovered_strategy=="bypass_token"``, the
   winning strategy persisted on the domain.
5. **Audit integrity.** The audit trail contains both the promote (step 2) and the
   access-recovery event (step 4), in a coherent (recovery-after-promote) order.
"""

from __future__ import annotations

import json
from pathlib import Path

from crawloop.access import build_http_client
from crawloop.config import AppConfig, DomainConfig
from crawloop.engine import Engine
from crawloop.llm import FakeCompleter
from crawloop.registry import Registry

SCHEMA = "Product@1"
FAMILY = "127.0.0.1/product_list"
# Matches the fixture server's /catalogue/ listing routes on the loopback host.
LISTING_PATTERN = r"^https?://127\.0\.0\.1.*/catalogue/.*"

_CASSETTE_PATH = Path(__file__).parent / "cassettes" / "selfheal.json"

# A CORRECT crawler for the NORMAL layout, registered as v1. It deliberately does
# NOT paginate (next_url=None) so the requested page yields exactly its own 3
# books — the clean "fast path serves the 3 page-1 books" assertion of step 1.
_V1_NORMAL = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksListV1(Crawler):
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


class FakeBrowserRunner:
    """A browser runner that is never expected to be used (the bypass_token rung
    clears the block over HTTP), but must satisfy the BrowserRunner port."""

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


def _load_cassette(base: str) -> list[str]:
    """Load the committed cassette and bind ``{BASE}`` to the server URL.

    Returns the ordered list of response strings ready for a positional
    :class:`FakeCompleter`. The cassette stays a hand-authored, committed file;
    the only runtime substitution is the ephemeral port via ``{BASE}``.
    """
    data = json.loads(_CASSETTE_PATH.read_text(encoding="utf-8"))
    return [entry["text"].replace("{BASE}", base) for entry in data["responses"]]


def _config_with_bypass() -> AppConfig:
    """Authorize 127.0.0.1 with an access ladder of backoff -> bypass_token.

    ``backoff`` (plain GET) is tried first and fails against the blocked server;
    ``bypass_token`` sends the ``x-test-bypass`` header whose value comes from the
    ``BYPASS`` env var (set to "ok" on the engine), matching the fixture server's
    bypass header — so recovery's second rung gets through. A high ``max_rps``
    keeps the localhost test instant.
    """
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=1000.0,
        render_js=False,
        access_strategies=[
            ("backoff", {}),
            ("bypass_token", {"header": "x-test-bypass", "value_env": "BYPASS"}),
        ],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


async def _noop_sleep(_seconds: float) -> None:
    """A no-op backoff so the recovery/transient sleeps never actually wait."""
    return None


def _book_names(items: list[dict]) -> set[str]:
    return {it["name"] for it in items}


def _book_prices(items: list[dict]) -> dict[str, str]:
    """Map name -> price string for an exact (name, price) assertion."""
    return {it["name"]: str(it["price"]) for it in items}


async def test_selfheal_and_access_recovery_e2e(fixture_server, tmp_path):
    """The full cycle: fast path -> drift+promote -> reuse healed -> recover."""
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"

    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    fixtures_dir = tmp_path / "fixtures"

    # Seed v1 (correct for NORMAL) as the family's active version.
    registry.upsert_family(
        FAMILY, [LISTING_PATTERN], SCHEMA, now="2026-06-13T00:00:00+00:00"
    )
    registry.add_version(FAMILY, _V1_NORMAL, now="2026-06-13T00:00:00+00:00")
    registry.set_active(FAMILY, 1)

    # Seed run-history with the three listing routes so the inline Loop's seeds
    # (recent_history + the requested URL) reach >= 3 reachable sample pages —
    # min_oracles=3 is the design's bound on the LLM oracle's error (§2/§9/§15).
    # The fixture server's '/' alone only harvests 2 pages, so three explicit
    # routes are needed; this ALSO pins the deterministic sample order the
    # ordered cassette is authored against (see the cassette notes).
    for route in ("/", "/catalogue/page-1.html", "/catalogue/page-2.html"):
        registry.record_history(
            FAMILY, f"{base}{route}", 1, [], now="2026-06-13T00:00:00+00:00"
        )

    completer = FakeCompleter(_load_cassette(base))

    async with build_http_client() as client:
        engine = Engine(
            _config_with_bypass(),
            registry,
            completer,
            client=client,
            browser_runner=FakeBrowserRunner(),
            fixtures_dir=fixtures_dir,
            run_loop_inline=True,
            env={"BYPASS": "ok"},
            sleep=_noop_sleep,
            now="2026-06-13T00:00:00+00:00",
        )

        # ---- STEP 1: fast path, NO LLM ----------------------------------- #
        fixture_server.mode = "normal"
        r1 = await engine.request(listing_url)
        assert r1.source == "registry"
        assert r1.family == FAMILY
        assert r1.used_version == 1
        assert r1.reason == "ok"
        # Exactly the three page-1 books, with their prices.
        assert _book_names(r1.items) == {
            "A Light in the Attic",
            "Tipping the Velvet",
            "Soumission",
        }
        assert _book_prices(r1.items) == {
            "A Light in the Attic": "51.77",
            "Tipping the Velvet": "53.74",
            "Soumission": "50.10",
        }
        # THE CRUX of step 1: the registry happy path costs no model call.
        assert completer.calls == []

        # ---- STEP 2: break layout -> fallback serves NOW + promote v2 ---- #
        fixture_server.mode = "mutated"
        calls_before = len(completer.calls)
        r2 = await engine.request(listing_url)

        # Served NOW via T2 with the correct 3 books, despite the drifted layout.
        assert r2.source == "fallback"
        assert r2.family == FAMILY
        assert r2.reason == "drift->fallback"
        assert _book_names(r2.items) == {
            "A Light in the Attic",
            "Tipping the Velvet",
            "Soumission",
        }
        # The inline Loop ran and PROMOTED a new version.
        assert r2.loop is not None
        assert r2.loop.ok is True
        assert r2.loop.escalated is False
        assert r2.loop.reason == "promoted"
        assert r2.loop.version == 2

        # A new ACTIVE version (v2) is on the ladder.
        ladder = registry.version_ladder(FAMILY)
        active = [v for v in ladder if v["status"] == "active"]
        assert len(active) == 1
        assert active[0]["n"] == 2

        # A "promote" audit entry was written for this family.
        promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
        assert len(promotes) == 1
        assert promotes[0]["data"]["to_version"] == 2

        # Golden fixtures were written for the family (one per loop sample).
        from crawloop.loop.promote import load_fixtures

        loaded = load_fixtures(fixtures_dir, FAMILY)
        assert len(loaded) == 3

        # The whole cassette was consumed in step 2: 1 fallback oracle + 3 loop
        # oracles + 2 codegen = 6 calls (and not one more — no repair fired).
        assert len(completer.calls) - calls_before == 6

        # ---- STEP 3: reuse the healed crawler, fast again, NO new LLM ---- #
        calls_before = len(completer.calls)
        r3 = await engine.request(listing_url)
        assert r3.source == "registry"
        assert r3.used_version == 2  # the healed v2 serves deterministically
        # v2 parses the mutated layout; it paginates (page-1 -> page-2) so it
        # gathers all four books across the two mutated pages.
        assert _book_names(r3.items) == {
            "A Light in the Attic",
            "Tipping the Velvet",
            "Soumission",
            "Sharp Objects",
        }
        # THE CRUX of step 3: the healed crawler needs no model — zero new calls.
        assert len(completer.calls) - calls_before == 0

        # ---- STEP 4: blocked -> access recovery heals fetching ----------- #
        fixture_server.mode = "blocked"
        calls_before = len(completer.calls)
        r4 = await engine.request(listing_url)
        assert r4.source == "recovered"
        assert r4.recovered_strategy == "bypass_token"
        assert r4.family == FAMILY
        # After recovery the ladder retried and served the books. NOTE: the
        # fixture server's blocked+bypass mode serves the NORMAL layout, so the
        # mutated-layout v2 finds nothing and the ladder falls back to v1 (the
        # normal-layout crawler), which serves page-1's 3 books. The headline
        # point of step 4 is that recovery healed *fetching* (the block cleared
        # and items came back); which version then validates is the executor
        # ladder doing its job on top of a now-unblocked fetch.
        assert _book_names(r4.items) == {
            "A Light in the Attic",
            "Tipping the Velvet",
            "Soumission",
        }
        # Recovery is not extraction: no model call.
        assert len(completer.calls) - calls_before == 0
        # The winning strategy is persisted on the domain's access store.
        assert registry.get_working_strategy("127.0.0.1") == "bypass_token"

    # ---- STEP 5: audit integrity ----------------------------------------- #
    audit = registry.read_audit()
    events = [e["event"] for e in audit]
    # Both the promotion (step 2) and an access-recovery event (step 4) are
    # recorded.
    assert "promote" in events
    assert "access_recovered" in events
    # read_audit is newest-first; recovery happened AFTER the promote, so it
    # appears at an earlier (newer) index -> coherent ordering.
    assert events.index("access_recovered") < events.index("promote")
