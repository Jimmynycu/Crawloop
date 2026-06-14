"""Tests for the engine orchestration (Task 10.2): the ``request()`` path.

The engine wires the whole §8 runtime flow together: authorize -> route ->
(known family: run the registry ladder, and on failure classify into
drift/blocked/transient/gone) or (unknown family: bootstrap). These tests drive
that path against the REAL components — the :class:`FixtureServer` behind a real
:class:`RealFetchContext` authorizing only ``127.0.0.1``, a real in-memory
:class:`Registry`, the real executor/validator/recovery — with the ONLY fake
being the :class:`FakeCompleter`. So no real model and no network beyond
localhost.

Each branch gets a focused test:

* **registry fast path** — a pre-seeded family whose active crawler parses the
  NORMAL layout serves directly, and the FakeCompleter is asserted NEVER called
  (no LLM on the happy path).
* **drift -> fallback** — flip the server to MUTATED so the registry crawler
  yields nothing; the engine serves via T2 (direct_extract, scripted oracle) and
  schedules the regeneration loop. The FakeCompleter IS called for the fallback.
* **blocked -> recovered** — server blocked; a bypass_token strategy whose env
  value matches the fixture header lets recovery through, the engine retries the
  ladder, and serves with source "recovered".
* **unauthorized** — an off-allowlist URL raises ``UnauthorizedDomain`` and is
  NEVER routed into healing.
* **unknown family + no schema** — raises ``EngineError``.
* **unknown family + schema** — bootstraps: serves via T2 and registers a family.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from crawloop.access import FetchBlocked, FetchError, build_http_client
from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain
from crawloop.engine import Engine, EngineError, RequestResult, _derive_pattern
from crawloop.executor import AllVersionsFailed
from crawloop.llm import FakeCompleter
from crawloop.registry import Registry

SCHEMA = "Product@1"
FAMILY = "127.0.0.1/product_list"
# A regex that matches the fixture server's listing routes on the loopback host.
LISTING_PATTERN = r"^https?://127\.0\.0\.1.*/catalogue/.*"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


# A CORRECT books crawler matching the fixture server's NORMAL layout. Used as
# the active registry version for the fast-path and drift tests.
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
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''


def _local_config(access_strategies=None) -> AppConfig:
    """Authorize the FixtureServer host (``127.0.0.1``) with a fast rate limit."""
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=1000.0,
        render_js=False,
        access_strategies=access_strategies or [("plain", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _seed_correct_family(registry: Registry) -> None:
    """Register the correct crawler as the active version of FAMILY, with a
    url_pattern matching the listing routes."""
    registry.upsert_family(FAMILY, [LISTING_PATTERN], SCHEMA)
    registry.add_version(FAMILY, _CORRECT)  # v1
    registry.set_active(FAMILY, 1)


def _engine(
    config: AppConfig,
    registry: Registry,
    completer: FakeCompleter,
    client: httpx.AsyncClient,
    tmp_path,
    *,
    run_loop_inline: bool = False,
) -> Engine:
    return Engine(
        config,
        registry,
        completer,
        client=client,
        browser_runner=FakeBrowserRunner(),
        fixtures_dir=tmp_path / "fixtures",
        snapshots_dir=tmp_path / "snapshots",
        run_loop_inline=run_loop_inline,
        now="2026-06-13T00:00:00+00:00",
    )


def _books_oracle(base: str, route: str) -> str:
    """The oracle JSON for one of the fixture server's listing routes.

    ``route`` is the absolute base the relative ``catalogue/...`` hrefs resolve
    against. ``/`` and ``/catalogue/page-1.html`` both serve the same 3 page-1
    books; only the absolutized urls differ by base path.
    """
    return json.dumps([
        {"name": "A Light in the Attic", "price": "51.77", "in_stock": True,
         "url": f"{route}catalogue/a-light-in-the-attic/index.html"},
        {"name": "Tipping the Velvet", "price": "53.74", "in_stock": True,
         "url": f"{route}catalogue/tipping-the-velvet/index.html"},
        {"name": "Soumission", "price": "50.10", "in_stock": False,
         "url": f"{route}catalogue/soumission/index.html"},
    ])


# --------------------------------------------------------------------------- #
# Fast path: known family, server normal -> source="registry", NO LLM call
# --------------------------------------------------------------------------- #


async def test_request_registry_fast_path_no_llm(fixture_server, tmp_path):
    """A known family whose active crawler parses the live page serves directly
    from the registry ladder, with items present and the FakeCompleter NEVER
    called — proving the happy path costs no LLM."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])  # must never be called on the happy path

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert isinstance(result, RequestResult)
    assert result.source == "registry"
    assert result.family == FAMILY
    assert result.used_version == 1
    assert result.reason == "ok"
    assert len(result.items) >= 1
    assert {it["name"] for it in result.items} >= {"A Light in the Attic"}
    # The crux: no model call on the registry happy path.
    assert completer.calls == []


# --------------------------------------------------------------------------- #
# Drift -> fallback: server mutated, registry crawler fails -> source="fallback"
# --------------------------------------------------------------------------- #


async def test_request_drift_serves_via_fallback_and_schedules_loop(
    fixture_server, tmp_path
):
    """When the live page drifted (MUTATED) the active registry crawler yields
    nothing -> AllVersionsFailed -> classified DRIFT. The engine serves NOW via
    T2 (direct_extract with the scripted oracle) and returns source="fallback"
    with items present; the FakeCompleter WAS called for the extraction.

    run_loop_inline=False keeps the test focused on the fallback serve: the loop
    is scheduled (a background task) rather than run, and ``result.loop`` is None.
    """
    fixture_server.mode = "mutated"
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    # The fallback fetches `listing_url` (served under /catalogue/), so relative
    # hrefs absolutize against `.../catalogue/`. One oracle response is enough.
    completer = FakeCompleter([_books_oracle(base, f"{base}/catalogue/")])

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=False,
        )
        result = await engine.request(listing_url)

    assert result.source == "fallback"
    assert result.family == FAMILY
    assert result.reason == "drift->fallback"
    assert len(result.items) == 3
    assert {it["name"] for it in result.items} == {
        "A Light in the Attic", "Tipping the Velvet", "Soumission",
    }
    # The fallback extraction reached the model exactly once.
    assert len(completer.calls) == 1
    # The loop was scheduled, not awaited (focused test).
    assert result.loop is None


async def test_request_drift_runs_loop_inline_and_attaches_result(
    fixture_server, tmp_path
):
    """With run_loop_inline=True the engine AWAITS the regeneration Loop and
    attaches its LoopResult to the response (rather than scheduling it). Here a
    single seed yields only 2 reachable samples, so the inline Loop escalates
    ("insufficient oracles") — a deterministic LoopResult that proves the inline
    plumbing end-to-end (the awaited result is surfaced on result.loop). The full
    inline PROMOTION cycle is covered by the run_loop driver test + M11 E2E.

    Script: one serving oracle (the drift fallback) + two loop-sample oracles
    (page-1 harvests itself + page-2); the Loop escalates before any codegen, so
    no candidate response is scripted.
    """
    fixture_server.mode = "mutated"
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([
        _books_oracle(base, f"{base}/catalogue/"),  # serving fallback
        _books_oracle(base, f"{base}/catalogue/"),  # loop sample 0 (page-1)
        _books_oracle(base, f"{base}/catalogue/"),  # loop sample 1 (page-2)
    ])

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=True,
        )
        result = await engine.request(listing_url)

    assert result.source == "fallback"
    assert len(result.items) == 3  # served via T2 before healing
    # The inline Loop ran and its result is attached (escalated, deterministically).
    assert result.loop is not None
    assert result.loop.escalated is True
    assert "oracle" in result.loop.reason.lower()
    # 1 serving oracle + 2 loop-sample oracles; no codegen (escalated first).
    assert len(completer.calls) == 3


# --------------------------------------------------------------------------- #
# I1: drift/bootstrap re-fetch failures must be CONTAINED inside request()
# (a known-family request returns a RequestResult; only UnauthorizedDomain
# propagates) — the re-fetch in _serve_drift / _bootstrap is OUTSIDE the
# ExtractionFailed try, so a FetchBlocked/FetchError there would otherwise
# escape raw, contradicting the §8 contract.
# --------------------------------------------------------------------------- #


async def test_drift_refetch_fetcherror_is_contained(
    fixture_server, tmp_path, monkeypatch
):
    """DRIFT then the T2 re-fetch raises FetchError: request() must NOT propagate
    it — it returns a RequestResult (source="fallback", reason mentions the
    re-fetch failure) and does not trigger the loop (we couldn't even fetch)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])  # T2 never reached (re-fetch fails first)

    import crawloop.engine as engine_mod

    async def drift_run_family(*args, **kwargs):
        raise AllVersionsFailed(
            family=FAMILY, reason="every version failed (drift)", last_report=None
        )

    monkeypatch.setattr(engine_mod, "run_family", drift_run_family)

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        # The drift re-fetch (ctx.fetch) blows up with a transport-style error.
        async def boom_fetch(url):
            raise FetchError(status=None, cause=TimeoutError("refetch timed out"))

        monkeypatch.setattr(engine._ctx, "fetch", boom_fetch)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert isinstance(result, RequestResult)
    assert result.source == "fallback"
    assert result.family == FAMILY
    assert result.items == []
    assert "refetch failed" in result.reason.lower()
    # We never reached extraction nor triggered the loop.
    assert completer.calls == []
    assert result.loop is None


async def test_drift_refetch_blocked_routes_to_recovery(
    fixture_server, tmp_path, monkeypatch
):
    """DRIFT then the T2 re-fetch raises FetchBlocked: a block surfacing now is an
    ACCESS problem, so request() routes to the recovery path (source="recovered")
    rather than propagating the FetchBlocked.

    The server is BLOCKED so the real recovery ladder genuinely needs the
    ``bypass_token`` rung (plain ``backoff`` fails the block); the bypass header's
    value comes from env and matches the fixture's ``x-test-bypass=ok``."""
    fixture_server.mode = "blocked"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])  # recovery is not extraction

    import crawloop.engine as engine_mod

    # The first run_family (the ladder) drifts; recover_access then succeeds via
    # the bypass rung, and the retried run_family serves against the now-unblocked
    # server (blocked+bypass mode serves the NORMAL layout).
    real_run_family = engine_mod.run_family
    calls = {"n": 0}

    async def drift_then_real(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AllVersionsFailed(family=FAMILY, reason="drift", last_report=None)
        return await real_run_family(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_family", drift_then_real)

    bypass = ("bypass_token", {"header": "x-test-bypass", "value_env": "BYPASS"})
    config = _local_config([("backoff", {}), bypass])

    async with build_http_client() as client:
        engine = Engine(
            config, registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            env={"BYPASS": "ok"},
            now="2026-06-13T00:00:00+00:00",
        )

        # Only the drift re-fetch (the engine's ctx.fetch) raises FetchBlocked;
        # recovery uses the strategy ladder through the shared guard, not this.
        async def blocked_fetch(url):
            raise FetchBlocked(status=429, marker="rate")

        monkeypatch.setattr(engine._ctx, "fetch", blocked_fetch)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    # The drift re-fetch was a block -> recovery handled it, not a raw raise.
    assert result.source == "recovered"
    assert result.recovered_strategy == "bypass_token"
    assert result.family == FAMILY
    assert completer.calls == []


async def test_drift_refetch_unauthorized_propagates(
    fixture_server, tmp_path, monkeypatch
):
    """DRIFT then the T2 re-fetch raises UnauthorizedDomain (an off-list redirect
    mid re-fetch): this is a HARD policy stop and MUST propagate out of
    request(), never be contained as a fallback or routed into recovery."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    async def drift_run_family(*args, **kwargs):
        raise AllVersionsFailed(family=FAMILY, reason="drift", last_report=None)

    monkeypatch.setattr(engine_mod, "run_family", drift_run_family)

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)

        async def offlist_fetch(url):
            raise UnauthorizedDomain("off-list redirect on re-fetch")

        monkeypatch.setattr(engine._ctx, "fetch", offlist_fetch)
        with pytest.raises(UnauthorizedDomain):
            await engine.request(f"{base}/catalogue/page-1.html")
    assert completer.calls == []


async def test_bootstrap_refetch_fetcherror_is_contained(
    fixture_server, tmp_path, monkeypatch
):
    """BOOTSTRAP then the T2 fetch raises FetchError: request() returns a
    RequestResult (source="bootstrap", reason mentions the fetch failure) WITHOUT
    registering a family or triggering the loop — we don't create a family we
    couldn't even fetch."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    completer = FakeCompleter([])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)

        async def boom_fetch(url):
            raise FetchError(status=None, cause=TimeoutError("bootstrap fetch failed"))

        monkeypatch.setattr(engine._ctx, "fetch", boom_fetch)
        result = await engine.request(listing_url, schema=SCHEMA)

    assert isinstance(result, RequestResult)
    assert result.source == "bootstrap"
    assert result.items == []
    assert "fetch failed" in result.reason.lower()
    assert result.loop is None
    # CRUX: no family was registered (we never fetched it), and no model call.
    assert registry.all_families() == []
    assert completer.calls == []


async def test_bootstrap_refetch_unauthorized_propagates(
    fixture_server, tmp_path, monkeypatch
):
    """BOOTSTRAP then the fetch raises UnauthorizedDomain: a hard policy stop must
    propagate, and still no family is registered."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    completer = FakeCompleter([])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)

        async def offlist_fetch(url):
            raise UnauthorizedDomain("off-list redirect on bootstrap fetch")

        monkeypatch.setattr(engine._ctx, "fetch", offlist_fetch)
        with pytest.raises(UnauthorizedDomain):
            await engine.request(listing_url, schema=SCHEMA)
    assert registry.all_families() == []
    assert completer.calls == []


# --------------------------------------------------------------------------- #
# Blocked -> recovered: bypass token clears the block -> source="recovered"
# --------------------------------------------------------------------------- #


async def test_request_blocked_routes_to_recovery_then_retries(
    fixture_server, tmp_path
):
    """A blocked server raises FetchBlocked from the ladder -> classified
    BLOCKED_* -> the engine calls recover_access (NOT the healing loop). A
    bypass_token strategy whose env value matches the fixture header gets
    through; the engine retries run_family and serves source="recovered" with the
    winning strategy recorded and items present. The FakeCompleter is never
    touched (recovery is not extraction)."""
    fixture_server.mode = "blocked"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])  # recovery path must not call the model

    # The bypass rung's value comes from env; the fixture's bypass header is
    # x-test-bypass=ok. backoff(plain) is tried first and fails the block.
    bypass = ("bypass_token", {"header": "x-test-bypass", "value_env": "BYPASS"})
    config = _local_config([("backoff", {}), bypass])

    async with build_http_client() as client:
        engine = Engine(
            config, registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            env={"BYPASS": "ok"},
            now="2026-06-13T00:00:00+00:00",
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "recovered"
    assert result.recovered_strategy == "bypass_token"
    assert result.family == FAMILY
    assert len(result.items) >= 1
    assert completer.calls == []
    # The winning strategy was persisted on the domain via the registry store.
    assert registry.get_working_strategy("127.0.0.1") == "bypass_token"


async def test_request_blocked_unrecoverable_escalates(fixture_server, tmp_path):
    """When recovery cannot get through (only backoff, no bypass), the engine
    escalates: returns empty items with a "blocked" reason, source="recovered",
    and does NOT loop forever or fall into healing."""
    fixture_server.mode = "blocked"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    config = _local_config([("backoff", {})])  # no rung clears the block

    async with build_http_client() as client:
        engine = Engine(
            config, registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            now="2026-06-13T00:00:00+00:00",
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "recovered"
    assert result.items == []
    assert "blocked" in result.reason.lower()
    assert completer.calls == []


# --------------------------------------------------------------------------- #
# I2: ONE GuardedClient => central per-host rate limiting. The engine must NOT
# build its own second GuardedClient for recovery; it shares the context's, so a
# single host's fast-path fetch and its recover_access go through the SAME
# per-host RateLimiter (not two independent caches that allow ~2x max_rps).
# --------------------------------------------------------------------------- #


async def test_engine_shares_one_guarded_client_for_recovery(
    fixture_server, tmp_path, monkeypatch
):
    """The engine exposes no separate guarded client; recovery uses the context's
    GuardedClient, so there is exactly ONE per-host RateLimiter cache. We capture
    the ``guarded`` recover_access is called with and assert it IS the context's
    guard — and that, after a fast-path fetch and a recovery on the same host, the
    very same RateLimiter instance backs that host on the shared guard."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    host = "127.0.0.1"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    seen = {}

    async def capture_recover(url, **kwargs):
        seen["guarded"] = kwargs["guarded"]
        from crawloop.loop.access_recovery import RecoveryResult

        return RecoveryResult(ok=False, strategy=None, rounds=1)

    monkeypatch.setattr(engine_mod, "recover_access", capture_recover)

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        # The engine must NOT carry its own separate guarded client (I2).
        assert not hasattr(engine, "_guarded")

        # A fast-path fetch builds the host's limiter on the context's guard.
        await engine._ctx.fetch(f"{base}/catalogue/page-1.html")
        limiter_after_fetch = engine._ctx.guarded._limiters[host]

        # Force a block so recovery is invoked, capturing the guarded it gets.
        async def blocked_fetch(url):
            raise FetchBlocked(status=429, marker="rate")

        monkeypatch.setattr(engine._ctx, "fetch", blocked_fetch)
        await engine.request(f"{base}/catalogue/page-1.html")

    # Recovery received the context's OWN guard, not a second one.
    assert seen["guarded"] is engine._ctx.guarded
    # And the host's limiter is one shared instance (central rate limiting).
    assert engine._ctx.guarded._limiters[host] is limiter_after_fetch


# --------------------------------------------------------------------------- #
# Gone: a known family whose page 404s -> classified GONE, no regen, empty items
# --------------------------------------------------------------------------- #


async def test_request_gone_returns_empty_no_regen(fixture_server, tmp_path):
    """A URL a family matches but that 404s makes the crawler's fetch raise
    FetchError(404) -> classified GONE. The engine returns empty items with a
    "gone" reason from source="registry" and NEVER regenerates (no LLM)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    # A loose pattern so a non-existent route still routes to this family.
    registry.upsert_family(FAMILY, [r"^https?://127\.0\.0\.1.*"], SCHEMA)
    registry.add_version(FAMILY, _CORRECT)
    registry.set_active(FAMILY, 1)
    completer = FakeCompleter([])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        result = await engine.request(f"{base}/catalogue/no-such-page.html")

    assert result.source == "registry"
    assert result.family == FAMILY
    assert result.items == []
    assert result.reason == "gone"
    assert completer.calls == []  # GONE never heals


# --------------------------------------------------------------------------- #
# Transient: a retryable failure is retried up to the cap (instant via sleep)
# --------------------------------------------------------------------------- #


async def test_request_transient_retries_then_succeeds(
    fixture_server, tmp_path, monkeypatch
):
    """A TimeoutError from run_family classifies TRANSIENT; the engine retries
    (with an injected no-op sleep so the test is instant) and the second attempt
    succeeds -> source="registry", items present. Proves the retry path and that
    the backoff sleep is injectable."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    real_run_family = engine_mod.run_family
    calls = {"n": 0}

    async def flaky_run_family(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("transient hiccup")
        return await real_run_family(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_family", flaky_run_family)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with build_http_client() as client:
        engine = Engine(
            _local_config(), registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            sleep=fake_sleep,
            now="2026-06-13T00:00:00+00:00",
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "registry"
    assert len(result.items) >= 1
    assert calls["n"] == 2  # failed once, retried once, succeeded
    assert len(slept) == 1  # one backoff between the two attempts
    assert completer.calls == []


async def test_request_transient_exhausts_retries(
    fixture_server, tmp_path, monkeypatch
):
    """When every attempt raises a transient failure, the engine gives up after
    max_transient_retries (capped — no infinite loop) and returns empty items
    with a "transient" reason. The injected sleep keeps it instant."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    calls = {"n": 0}

    async def always_transient(*args, **kwargs):
        calls["n"] += 1
        raise TimeoutError("persistent hiccup")

    monkeypatch.setattr(engine_mod, "run_family", always_transient)

    async def fake_sleep(seconds: float) -> None:
        return None

    async with build_http_client() as client:
        engine = Engine(
            _local_config(), registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            sleep=fake_sleep,
            max_transient_retries=2,
            now="2026-06-13T00:00:00+00:00",
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.items == []
    assert "transient" in result.reason.lower()
    # 1 initial attempt + 2 retries = 3 total, then give up (capped).
    assert calls["n"] == 3
    assert completer.calls == []


async def test_request_transient_retry_unauthorized_propagates(
    fixture_server, tmp_path, monkeypatch
):
    """If a transient retry later hits an off-list redirect (UnauthorizedDomain),
    it must PROPAGATE out of request() rather than be swallowed as a transient
    give-up — the hard policy stop is never healed, even mid-retry."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_correct_family(registry)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("transient first")
        raise UnauthorizedDomain("off-list redirect on retry")

    monkeypatch.setattr(engine_mod, "run_family", flaky)

    async def fake_sleep(seconds: float) -> None:
        return None

    async with build_http_client() as client:
        engine = Engine(
            _local_config(), registry, completer,
            client=client, browser_runner=FakeBrowserRunner(),
            fixtures_dir=tmp_path / "fixtures",
            sleep=fake_sleep,
            now="2026-06-13T00:00:00+00:00",
        )
        with pytest.raises(UnauthorizedDomain):
            await engine.request(f"{base}/catalogue/page-1.html")
    assert completer.calls == []


# --------------------------------------------------------------------------- #
# Unauthorized: off-allowlist URL propagates, never healed
# --------------------------------------------------------------------------- #


async def test_request_unauthorized_propagates(tmp_path):
    """An off-allowlist URL raises UnauthorizedDomain straight out of request()
    — the hard policy stop is never routed into routing/healing/recovery."""
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    completer = FakeCompleter([])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        with pytest.raises(UnauthorizedDomain):
            await engine.request("https://evil.com/x")
    # Nothing was extracted and the model was never reached.
    assert completer.calls == []


# --------------------------------------------------------------------------- #
# Unknown family + no schema -> EngineError
# --------------------------------------------------------------------------- #


async def test_request_unknown_family_without_schema_raises(fixture_server, tmp_path):
    """An authorized URL that matches no family AND no schema arg is given ->
    EngineError (we will not bootstrap blind)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    completer = FakeCompleter([])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        with pytest.raises(EngineError):
            await engine.request(f"{base}/catalogue/page-1.html")


# --------------------------------------------------------------------------- #
# Unknown family + schema -> bootstrap (serve via T2, register a family)
# --------------------------------------------------------------------------- #


async def test_request_unknown_family_with_schema_bootstraps(fixture_server, tmp_path):
    """An authorized URL matching no family, but given a schema, bootstraps: the
    engine serves NOW via T2 (scripted oracle) and registers a new family with a
    derived url_pattern. source="bootstrap", items served.

    run_loop_inline=False keeps the heavy regeneration loop out of this test (it
    is scheduled, not awaited) — the request-path branch is what is under test.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    listing_url = f"{base}/catalogue/page-1.html"
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    completer = FakeCompleter([_books_oracle(base, f"{base}/catalogue/")])

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=False,
        )
        result = await engine.request(listing_url, schema=SCHEMA)

    assert result.source == "bootstrap"
    assert result.reason == "new family"
    assert len(result.items) == 3
    assert result.family is not None
    # A family row was registered for the bootstrapped family.
    assert registry.get_family(result.family) is not None
    assert registry.get_family(result.family)["schema_ref"] == SCHEMA
    # The fallback extraction reached the model once.
    assert len(completer.calls) == 1


# --------------------------------------------------------------------------- #
# M4: the derived bootstrap pattern must match its OWN seed url (and siblings),
# else the very next request to that seed re-bootstraps instead of routing to
# the family just registered. The single-segment seed `.../p` must match.
# --------------------------------------------------------------------------- #


def test_derive_pattern_matches_its_own_pathless_seed():
    """A single-path-segment seed url must match its own derived pattern (so the
    next request routes to the family, not re-bootstrap) AND a sibling `.../p/2`.
    The trailing slash after the segment must be OPTIONAL."""
    import re

    seed = "http://host.test/p"
    pattern = _derive_pattern(seed)
    assert re.search(pattern, seed) is not None  # matches its own seed
    assert re.search(pattern, "http://host.test/p/2") is not None  # and a sibling
    # Sanity: a different first segment on the same host does NOT match.
    assert re.search(pattern, "http://host.test/other") is None


# --------------------------------------------------------------------------- #
# I3: per-family Loop dedup (§8 "one in-flight job per family"). In background
# mode two concurrent drift requests for the SAME family must not each spawn a
# full run_loop (double promotion + double LLM spend). The engine guards the
# background branch with an in-flight set; it also marks the family
# "regenerating" while the loop runs and back to "healthy" on a clean end
# (leaving "escalated" alone) so status reflects reality (review M3).
# --------------------------------------------------------------------------- #


async def test_background_loop_dedups_per_family(tmp_path, monkeypatch):
    """Two `_trigger_loop` calls for the same family while the first is still in
    flight schedule only ONE background task; the second returns without
    scheduling. A stubbed run_loop blocks on an Event so the first stays
    in-flight deterministically (no real sleeps)."""
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    registry.upsert_family(FAMILY, [LISTING_PATTERN], SCHEMA)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    gate = asyncio.Event()
    runs = {"n": 0}

    async def blocking_run_loop(*args, **kwargs):
        runs["n"] += 1
        await gate.wait()  # hold the loop "in flight" until released
        return engine_mod.LoopResult(
            ok=True, version=2, rounds=1, escalated=False, reason="promoted"
        )

    monkeypatch.setattr(engine_mod, "run_loop", blocking_run_loop)

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=False,
        )
        # First trigger schedules a background task that blocks on the gate.
        r1 = await engine._trigger_loop(FAMILY, SCHEMA, seed_url=f"http://x/{FAMILY}")
        # Second trigger for the SAME family, while the first is in flight.
        r2 = await engine._trigger_loop(FAMILY, SCHEMA, seed_url=f"http://x/{FAMILY}")
        # Let the (single) scheduled task run to completion.
        gate.set()
        # Drain the background task(s) so the test leaves nothing pending.
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks))

    # Background mode returns None from _trigger_loop either way.
    assert r1 is None
    assert r2 is None
    # CRUX: the loop body ran exactly ONCE despite two triggers in flight.
    assert runs["n"] == 1
    # The in-flight family was cleared once the single loop finished.
    assert FAMILY not in engine._inflight_families


async def test_background_loop_marks_family_regenerating_then_healthy(
    tmp_path, monkeypatch
):
    """A background loop sets the family status to "regenerating" while it runs
    and back to "healthy" on a clean (non-escalated) end — observed via a stub
    that captures the status at run time and a post-run assertion."""
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    registry.upsert_family(FAMILY, [LISTING_PATTERN], SCHEMA)
    assert registry.get_family(FAMILY)["status"] == "healthy"
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    captured = {}

    async def run_loop_capture(*args, **kwargs):
        captured["status_during"] = registry.get_family(FAMILY)["status"]
        return engine_mod.LoopResult(
            ok=True, version=2, rounds=1, escalated=False, reason="promoted"
        )

    monkeypatch.setattr(engine_mod, "run_loop", run_loop_capture)

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=False,
        )
        await engine._trigger_loop(FAMILY, SCHEMA, seed_url=f"http://x/{FAMILY}")
        if engine._bg_tasks:
            await asyncio.gather(*list(engine._bg_tasks))

    # The family was marked "regenerating" while the loop ran...
    assert captured["status_during"] == "regenerating"
    # ...and reset to "healthy" after a clean (promoted) end.
    assert registry.get_family(FAMILY)["status"] == "healthy"


async def test_loop_leaves_escalated_status_when_loop_escalates(
    tmp_path, monkeypatch
):
    """When the loop ESCALATES, its own escalation already set the family status
    to "escalated"; the engine's status bookkeeping must NOT overwrite that back
    to "healthy". (The stub mimics the escalation side effect.)"""
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    registry.upsert_family(FAMILY, [LISTING_PATTERN], SCHEMA)
    completer = FakeCompleter([])

    import crawloop.engine as engine_mod

    async def run_loop_escalates(*args, **kwargs):
        # Mimic _escalate's side effect: the real run_loop marks the family
        # escalated on escalation.
        registry.set_family_status(FAMILY, "escalated")
        return engine_mod.LoopResult(
            ok=False, version=None, rounds=3, escalated=True,
            reason="max rounds exhausted",
        )

    monkeypatch.setattr(engine_mod, "run_loop", run_loop_escalates)

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path,
            run_loop_inline=True,  # inline so we can await the escalation directly
        )
        result = await engine._trigger_loop(FAMILY, SCHEMA, seed_url=f"http://x/{FAMILY}")

    assert result is not None
    assert result.escalated is True
    # The escalated status must survive (not be reset to "healthy").
    assert registry.get_family(FAMILY)["status"] == "escalated"
