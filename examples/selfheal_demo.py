#!/usr/bin/env python3
"""Self-healing crawler — the 60-second "wow", fully offline, no API key.

Run it:

    pip install -e ".[dev]"
    python examples/selfheal_demo.py

What you'll watch happen, narrated step by step:

    1. BOOTSTRAP  a crawler is registered and serves a books listing — for free
                  (straight from the registry, zero model calls).
    2. MUTATE     the website is silently redesigned: every CSS hook the crawler
                  relied on is renamed. The old crawler now extracts *nothing*.
    3. DETECT     the next request notices the drift and, instead of returning
                  junk, an LLM reads the raw HTML and serves the right records
                  RIGHT NOW (the "fallback" tier) so the caller never sees an
                  outage.
    4. REGENERATE in the same breath, a regeneration loop writes, sandboxes, and
                  validates a brand-new crawler for the redesigned page, then
                  PROMOTES it — no human in the loop.
    5. FREE AGAIN the next request is served by the healed crawler again straight
                  from the registry: zero model calls. The site broke and fixed
                  itself.

This is the EXACT machinery exercised by tests/test_selfheal_e2e.py, driven as a
story instead of as asserts. The only fake is the language model: a
``FakeCompleter`` replays a committed "cassette" of canned responses
(tests/cassettes/selfheal.json) — the same offline pattern the E2E uses — so the
demo is deterministic and needs no network and no API key. Everything else (the
engine, registry, executor, sandbox, validator, the local fixture web server) is
the real thing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Make the repo's tests/ importable so we can REUSE the real fixture server and
# the committed cassette rather than copy-pasting either of them here. This is
# the one bit of path wiring an example needs; everything below is public API.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from crawloop.access import build_http_client  # noqa: E402
from crawloop.config import AppConfig, DomainConfig  # noqa: E402
from crawloop.engine import Engine  # noqa: E402
from crawloop.llm import FakeCompleter  # noqa: E402
from crawloop.loop.promote import load_fixtures  # noqa: E402
from crawloop.registry import Registry  # noqa: E402
from tests.fixture_server.server import FixtureServer  # noqa: E402

SCHEMA = "Product@1"
FAMILY = "127.0.0.1/product_list"
# Matches the fixture server's /catalogue/ listing routes on the loopback host.
LISTING_PATTERN = r"^https?://127\.0\.0\.1.*/catalogue/.*"

# The committed cassette the E2E test replays — we read the very same file.
_CASSETTE_PATH = _REPO_ROOT / "tests" / "cassettes" / "selfheal.json"

# A CORRECT crawler for the ORIGINAL ("normal") layout, registered as v1. It is
# the same seed the E2E uses: brittle on purpose — it hard-codes the original
# CSS hooks (article.product_pod, .price_color, .availability), which is exactly
# what the redesign in step 2 breaks.
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


class _FakeBrowserRunner:
    """Satisfies the BrowserRunner port; never actually used in this demo."""

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


async def _noop_sleep(_seconds: float) -> None:
    """No-op backoff so nothing in the demo actually waits."""
    return None


def _load_cassette(base: str) -> list[str]:
    """Load the committed cassette and bind ``{BASE}`` to the live server URL.

    Identical to the helper in tests/test_selfheal_e2e.py: the cassette is a
    hand-authored, committed list of model responses; the only runtime
    substitution is the fixture server's ephemeral port via ``{BASE}``.
    """
    data = json.loads(_CASSETTE_PATH.read_text(encoding="utf-8"))
    return [entry["text"].replace("{BASE}", base) for entry in data["responses"]]


def _config() -> AppConfig:
    """Authorize the loopback host with a high rate limit (instant on localhost)."""
    dc = DomainConfig(domain="127.0.0.1", max_rps=1000.0, render_js=False)
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


# --- narration helpers ---------------------------------------------------- #

_BAR = "=" * 70


def _say(*lines: str) -> None:
    for line in lines:
        print(line)


def _step(n: int, title: str) -> None:
    print()
    print(_BAR)
    print(f"  STEP {n}  {title}")
    print(_BAR)


def _show_records(items: list[dict]) -> None:
    """Pretty-print the extracted records as an at-a-glance table."""
    if not items:
        print("    (no records — the crawler extracted nothing)")
        return
    rows = sorted(items, key=lambda it: it.get("name") or "")
    name_w = max(len(str(it.get("name"))) for it in rows)
    for it in rows:
        stock = "in stock" if it.get("in_stock") else "OUT of stock"
        print(f"    - {str(it.get('name')):<{name_w}}  £{it.get('price')!s:<6}  {stock}")


def _calls_delta(completer: FakeCompleter, before: int) -> int:
    return len(completer.calls) - before


async def _tell_story(work: Path) -> None:
    """Run the whole self-heal cycle against a fresh scratch ``work`` dir."""
    # The real local web server that plays the role of the target site. It serves
    # a 4-book catalogue and can flip its HTML layout at runtime with no restart.
    with FixtureServer() as site:
        base = site.url
        listing_url = f"{base}/catalogue/page-1.html"
        _say("", f"  Local fixture site running at {base}")

        registry = Registry(db_path=":memory:", crawlers_dir=work / "crawlers")
        fixtures_dir = work / "fixtures"

        # Seed v1 (correct for the ORIGINAL layout) as the family's active crawler.
        registry.upsert_family(
            FAMILY, [LISTING_PATTERN], SCHEMA, now="2026-06-14T00:00:00+00:00"
        )
        registry.add_version(FAMILY, _V1_NORMAL, now="2026-06-14T00:00:00+00:00")
        registry.set_active(FAMILY, 1)
        # Seed a little run-history so the regeneration loop has >= 3 sample pages
        # to learn from (the same seeds the E2E uses; this also pins the order the
        # committed cassette is authored against).
        for route in ("/", "/catalogue/page-1.html", "/catalogue/page-2.html"):
            registry.record_history(
                FAMILY, f"{base}{route}", 1, [], now="2026-06-14T00:00:00+00:00"
            )

        # The ONLY fake: a scripted model replaying the committed cassette.
        completer = FakeCompleter(_load_cassette(base))

        async with build_http_client() as client:
            engine = Engine(
                _config(),
                registry,
                completer,
                client=client,
                browser_runner=_FakeBrowserRunner(),
                fixtures_dir=fixtures_dir,
                run_loop_inline=True,  # heal synchronously so the story is linear
                sleep=_noop_sleep,
                now="2026-06-14T00:00:00+00:00",
            )

            # ---- STEP 1: bootstrap — serve for free ----------------------- #
            _step(1, "BOOTSTRAP — a crawler is live and serving for free")
            site.mode = "normal"
            _say(
                "  The site is in its ORIGINAL layout. A crawler (v1) is already",
                "  registered for it. We ask the engine for the listing page:",
                "",
                f"    GET {listing_url}",
            )
            r1 = await engine.request(listing_url)
            _say("", "  Records returned:")
            _show_records(r1.items)
            _say(
                "",
                f"  source       = {r1.source}   (straight from the registry)",
                f"  used_version = {r1.used_version}",
                f"  model calls  = {len(completer.calls)}   <- ZERO. The happy path is free.",
            )

            # ---- STEP 2: mutate the site ---------------------------------- #
            _step(2, "MUTATE — the website is redesigned overnight")
            site.mode = "mutated"
            _say(
                "  Same books, same prices — but every CSS hook the crawler relied",
                "  on has been renamed by the redesign:",
                "",
                "    article.product_pod  ->  div.card",
                "    p.price_color        ->  span.price-box",
                "    p.availability       ->  span.stock",
                "",
                "  The v1 crawler still 'runs', but it now matches nothing. In a",
                "  normal pipeline this is a silent outage: empty rows, no error.",
            )

            # ---- STEP 3 + 4: detect drift, serve now, regenerate ---------- #
            _step(3, "DETECT + REGENERATE — heal without an outage")
            _say(
                "  We ask for the same page again. The engine runs v1, sees it",
                "  extract nothing on a page that clearly has content, and classifies",
                "  this as DRIFT (a layout change) — not a real 'empty' result.",
                "",
                f"    GET {listing_url}",
            )
            calls_before = len(completer.calls)
            r2 = await engine.request(listing_url)
            _say("", "  Records returned (DURING the redesign, with no working crawler):")
            _show_records(r2.items)
            _say(
                "",
                f"  source = {r2.source}   <- an LLM read the raw HTML and served NOW",
                f"  reason = {r2.reason}",
                "",
                "  The caller never saw an outage. Meanwhile, in the SAME request, the",
                "  regeneration loop wrote a fresh crawler for the new layout,",
                "  sandboxed it, validated it against golden samples, and promoted it:",
            )
            assert r2.loop is not None  # inline loop ran
            _say(
                f"    loop.ok      = {r2.loop.ok}",
                f"    loop.reason  = {r2.loop.reason}",
                f"    new version  = v{r2.loop.version}   (now the active crawler)",
            )
            ladder = registry.version_ladder(FAMILY)
            active = [v for v in ladder if v["status"] == "active"]
            promotes = [e for e in registry.read_audit(FAMILY) if e["event"] == "promote"]
            loaded = load_fixtures(fixtures_dir, FAMILY)
            _say(
                "",
                "  Audit trail (what actually changed, recorded for you):",
                f"    active version on the ladder : v{active[0]['n']}",
                f"    'promote' audit entries      : {len(promotes)}"
                f"  (to v{promotes[0]['data']['to_version']})",
                f"    golden fixtures written      : {len(loaded)}",
                f"    model calls this request     : {_calls_delta(completer, calls_before)}"
                "  (1 to serve now + 3 to learn + 2 to write the crawler)",
            )

            # ---- STEP 5: free again --------------------------------------- #
            _step(5, "FREE AGAIN — the healed crawler serves the new layout")
            _say(
                "  The site is STILL in its redesigned layout. We ask one more time.",
                "  This time the freshly-promoted v2 handles it directly — and it even",
                "  follows pagination, so it gathers every book across both pages:",
                "",
                f"    GET {listing_url}",
            )
            calls_before = len(completer.calls)
            r3 = await engine.request(listing_url)
            _say("", "  Records returned:")
            _show_records(r3.items)
            _say(
                "",
                f"  source       = {r3.source}   (back to the registry)",
                f"  used_version = {r3.used_version}   <- the self-written crawler",
                f"  model calls  = {_calls_delta(completer, calls_before)}   <- ZERO again. "
                "Healed and free.",
            )


async def _run() -> int:
    _say(
        "",
        "  SELF-HEALING CRAWLER — live demo (offline, deterministic, no API key)",
        "",
        "  A website is about to be redesigned out from under a crawler. Watch the",
        "  crawler notice, keep serving correct data through the outage, write its",
        "  own replacement, and go back to running for free — with no human and no",
        "  real language model (a committed cassette stands in for the LLM).",
    )

    # A FRESH scratch dir per run (like the E2E's pytest tmp_path) holds the
    # generated crawler files + golden fixtures. Starting clean every time is what
    # keeps the demo deterministic and re-runnable: stale crawler state on disk
    # would change what the loop does and desync it from the committed cassette.
    work = Path(tempfile.mkdtemp(prefix="selfheal_demo_"))
    try:
        await _tell_story(work)
    finally:
        # Leave the machine as we found it (the artifacts were only for the demo).
        shutil.rmtree(work, ignore_errors=True)

    # ---- the punchline ---------------------------------------------------- #
    print()
    print(_BAR)
    _say(
        "  BEFORE  ->  the site changed and the brittle crawler broke (0 records).",
        "  DURING  ->  the engine served correct data anyway, with no outage.",
        "  AFTER   ->  it wrote + promoted a new crawler and runs for free again.",
        "",
        "  No network. No API key. The only stand-in was a committed cassette of",
        "  model responses — the exact same offline setup as the E2E test in",
        "  tests/test_selfheal_e2e.py. Everything else was the real engine.",
    )
    print(_BAR)
    print()
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
