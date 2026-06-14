"""Tests for the RealFetchContext, the strategy factory, and the typed fetch
errors (Task 4.3).

These run against the *real* :class:`FixtureServer` (authorizing its
``127.0.0.1`` host via a hand-built :class:`AppConfig`) so the HTTP path is
genuinely exercised, plus an in-memory :class:`AccessStore` and a fake
:class:`BrowserRunner`. Every httpx client a test owns is closed via
``async with`` so none leak.

The allowlist gate and per-host rate limit now live in the shared
:class:`GuardedClient` the context builds internally; tests that need to observe
rate-limiting install a :class:`CountingRateLimiter` into that guard's per-host
registry (``ctx._guarded._limiters``) before fetching.
"""

import httpx
import pytest
from parsel import Selector

from crawloop.access import (
    BackoffRetry,
    BrowserFetch,
    CaptchaSolver,
    FetchBlocked,
    FetchError,
    GuardedClient,
    HeaderFetch,
    PlainHTTP,
    RateLimiter,
    RealFetchContext,
    build_strategy,
)
from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    """In-memory :class:`BrowserRunner` returning canned HTML, recording calls."""

    def __init__(self, html: str = "<html>rendered</html>"):
        self._html = html
        self.calls: list[dict] = []

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        self.calls.append(
            {"url": url, "stealth": stealth, "wait_for": wait_for, "extra_headers": extra_headers}
        )
        return self._html


class InMemoryAccessStore:
    """In-memory :class:`AccessStore` (the shape M5's registry will satisfy)."""

    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


class CountingRateLimiter(RateLimiter):
    """A real :class:`RateLimiter` (no-op sleep) that records every acquire.

    Installed into a :class:`GuardedClient`'s per-host registry to prove a fetch
    went through the central rate-limit chokepoint."""

    def __init__(self, max_rps: float = 100.0):
        super().__init__(max_rps, sleep=self._noop)
        self.acquired: list[str] = []

    @staticmethod
    async def _noop(_seconds: float) -> None:
        return None

    async def acquire(self, domain: str) -> None:
        self.acquired.append(domain)
        await super().acquire(domain)


class _NullClient:
    """A do-nothing stand-in for an :class:`httpx.AsyncClient`, used only by the
    coercion-helper tests which never fetch. Holds the ``follow_redirects``
    attribute :class:`GuardedClient` sets and owns no resources (so it cannot
    leak)."""

    follow_redirects = False


def _spy_limiter_on(ctx: RealFetchContext, host: str) -> CountingRateLimiter:
    """Install and return a counting limiter for ``host`` on the context's guard
    so the test can assert the fetch was rate-limited centrally."""
    limiter = CountingRateLimiter()
    ctx._guarded._limiters[host] = limiter
    return limiter


def _guarded(client: httpx.AsyncClient, cfg: AppConfig) -> GuardedClient:
    return GuardedClient(client, cfg, env={})


def _local_config(
    *,
    render_js: bool = False,
    access_strategies: list[tuple[str, dict]] | None = None,
    max_rps: float = 100.0,
) -> AppConfig:
    """Authorize the FixtureServer host (``127.0.0.1``) with a chosen policy.

    Built directly (no YAML) per the task note: ``assert_authorized`` matches
    host exactly, and the fixture server's host is literally ``127.0.0.1``.
    """
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=max_rps,
        render_js=render_js,
        access_strategies=access_strategies if access_strategies is not None else [("backoff", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


# --------------------------------------------------------------------------- #
# build_strategy factory — kind -> concrete strategy
# --------------------------------------------------------------------------- #


async def test_factory_plain():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy("plain", {}, guarded=guarded, browser_runner=FakeBrowserRunner())
        assert isinstance(strat, PlainHTTP)
        assert strat.name == "plain"


async def test_factory_backoff_wraps_plain():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy("backoff", {}, guarded=guarded, browser_runner=FakeBrowserRunner())
        assert isinstance(strat, BackoffRetry)
        assert isinstance(strat.inner, PlainHTTP)
        assert strat.name == "backoff"


async def test_factory_browser_non_stealth():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy("browser", {}, guarded=guarded, browser_runner=FakeBrowserRunner())
        assert isinstance(strat, BrowserFetch)
        assert strat.stealth is False
        assert strat.name == "browser"


async def test_factory_stealth_browser():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "stealth_browser", {}, guarded=guarded, browser_runner=FakeBrowserRunner()
        )
        assert isinstance(strat, BrowserFetch)
        assert strat.stealth is True
        assert strat.name == "stealth_browser"


async def test_factory_session_reads_cookie_from_env():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "session",
            {"creds_env": "MY_SESSION"},
            guarded=guarded,
            browser_runner=FakeBrowserRunner(),
            env={"MY_SESSION": "session=abc123"},
        )
        assert isinstance(strat, HeaderFetch)
        assert strat.name == "session"
        # documented choice: the credential is sent as a Cookie header.
        assert strat._headers["cookie"] == "session=abc123"


async def test_factory_session_missing_env_still_constructs():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "session", {"creds_env": "ABSENT"}, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env={},
        )
        assert isinstance(strat, HeaderFetch)
        # empty value -> will simply not clear the block (no live login in POC).
        assert strat._headers["cookie"] == ""


async def test_factory_session_no_creds_env_key():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "session", {}, guarded=guarded, browser_runner=FakeBrowserRunner(), env={}
        )
        assert isinstance(strat, HeaderFetch)
        assert strat._headers["cookie"] == ""


async def test_factory_bypass_token_reads_header_from_env():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "bypass_token",
            {"header": "x-waf-bypass", "value_env": "WAF"},
            guarded=guarded,
            browser_runner=FakeBrowserRunner(),
            env={"WAF": "secret"},
        )
        assert isinstance(strat, HeaderFetch)
        assert strat.name == "bypass_token"
        assert strat._headers["x-waf-bypass"] == "secret"


async def test_factory_bypass_token_missing_env_is_empty_value():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        strat = build_strategy(
            "bypass_token",
            {"header": "x-waf-bypass", "value_env": "ABSENT"},
            guarded=guarded,
            browser_runner=FakeBrowserRunner(),
            env={},
        )
        assert strat._headers["x-waf-bypass"] == ""  # won't clear the block


async def test_factory_captcha_solver_authorized_flag():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        on = build_strategy(
            "captcha_solver", {"authorized": True}, guarded=guarded,
            browser_runner=FakeBrowserRunner(),
        )
        off = build_strategy(
            "captcha_solver", {}, guarded=guarded, browser_runner=FakeBrowserRunner()
        )
        assert isinstance(on, CaptchaSolver) and on.authorized is True
        assert isinstance(off, CaptchaSolver) and off.authorized is False


async def test_factory_unknown_kind_raises():
    async with httpx.AsyncClient() as client:
        guarded = _guarded(client, _local_config())
        with pytest.raises(ValueError):
            build_strategy("nope", {}, guarded=guarded, browser_runner=FakeBrowserRunner())


# --------------------------------------------------------------------------- #
# RealFetchContext.fetch — allowlist, rate-limit, single fast strategy
# --------------------------------------------------------------------------- #


async def test_fetch_offlist_raises_unauthorized_and_is_not_swallowed():
    """An off-list host must raise UnauthorizedDomain straight through; it must
    NOT be caught and remapped to FetchError/FetchBlocked."""
    cfg = _local_config()  # only 127.0.0.1 authorized
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(UnauthorizedDomain):
            await ctx.fetch("https://evil.com/x")


async def test_fetch_happy_path_returns_html_and_rate_limits(fixture_server):
    fixture_server.mode = "normal"
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        limiter = _spy_limiter_on(ctx, "127.0.0.1")  # spy on the guard's limiter
        html = await ctx.fetch(f"{fixture_server.url}/catalogue/page-1.html")

    assert len(Selector(text=html).css("article.product_pod")) == 3
    # the central per-host limiter (inside GuardedClient) was consulted
    assert limiter.acquired == ["127.0.0.1"]


async def test_fetch_blocked_default_ladder_raises_fetch_blocked(fixture_server):
    """Blocked fixture, no saved strategy, default ladder (backoff->plain) ->
    FetchBlocked with the challenge marker. fetch does NOT walk the ladder."""
    fixture_server.mode = "blocked"
    cfg = _local_config(access_strategies=[("backoff", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(FetchBlocked) as ei:
            await ctx.fetch(f"{fixture_server.url}/catalogue/page-1.html")
    assert ei.value.status == 403
    assert ei.value.marker == "challenge"


async def test_fetch_honors_saved_working_strategy(fixture_server):
    """A saved working strategy is used as the fast path, beating the default.

    bypass_token (header x-test-bypass=ok, env-injected) clears the block; the
    default backoff(plain) would NOT. Saving the winner first proves reuse.
    """
    fixture_server.mode = "blocked"
    cfg = _local_config(
        access_strategies=[
            ("backoff", {}),
            ("bypass_token", {"header": "x-test-bypass", "value_env": "BYPASS"}),
        ]
    )
    store = InMemoryAccessStore()
    store.set_working_strategy("127.0.0.1", "bypass_token")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner(),
            env={"BYPASS": "ok"},
        )
        html = await ctx.fetch(f"{fixture_server.url}/catalogue/page-1.html")
    assert len(Selector(text=html).css("article.product_pod")) == 3


async def test_fetch_error_status_raises_fetch_error(fixture_server):
    """A terminal error (e.g. 404) from the fast strategy -> FetchError(status)."""
    fixture_server.mode = "normal"
    cfg = _local_config(access_strategies=[("plain", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(FetchError) as ei:
            await ctx.fetch(f"{fixture_server.url}/catalogue/page-999.html")
    assert ei.value.status == 404


async def test_fetch_default_prefers_browser_when_render_js(fixture_server):
    """When the domain wants JS and the first configured strategy is not a
    browser one, fetch's fast default is a browser strategy (uses the runner)."""
    fixture_server.mode = "blocked"  # HTTP would be blocked; browser runner is faked ok
    runner = FakeBrowserRunner(html="<html><article class='product_pod'>x</article></html>")
    cfg = _local_config(render_js=True, access_strategies=[("backoff", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=runner
        )
        html = await ctx.fetch(f"{fixture_server.url}/catalogue/page-1.html")
    assert runner.calls, "browser runner should have been used for a render_js domain"
    assert "product_pod" in html


# --------------------------------------------------------------------------- #
# RealFetchContext.fetch_rendered — forces a browser
# --------------------------------------------------------------------------- #


async def test_fetch_rendered_uses_browser_and_passes_wait_for(fixture_server):
    runner = FakeBrowserRunner(html="<html>rendered-body</html>")
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=runner
        )
        html = await ctx.fetch_rendered(
            f"{fixture_server.url}/catalogue/page-1.html", wait_for=".product_pod"
        )
    assert html == "<html>rendered-body</html>"
    assert runner.calls[0]["stealth"] is False  # domain didn't ask for stealth
    assert runner.calls[0]["wait_for"] == ".product_pod"


async def test_fetch_rendered_uses_stealth_when_domain_configures_it(fixture_server):
    runner = FakeBrowserRunner()
    cfg = _local_config(access_strategies=[("stealth_browser", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=runner
        )
        await ctx.fetch_rendered(f"{fixture_server.url}/catalogue/page-1.html")
    assert runner.calls[0]["stealth"] is True


async def test_fetch_rendered_offlist_raises_unauthorized():
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(UnauthorizedDomain):
            await ctx.fetch_rendered("https://evil.com/x")


# --------------------------------------------------------------------------- #
# Coercion helpers delegate to the contract module
# --------------------------------------------------------------------------- #


def _ctx_for_helpers() -> RealFetchContext:
    # Coercion helpers never fetch, so a resource-less _NullClient is enough and
    # cannot leak (the real GuardedClient just toggles follow_redirects on it).
    return RealFetchContext(
        _local_config(), InMemoryAccessStore(),
        client=_NullClient(), browser_runner=FakeBrowserRunner(),
    )


def test_absolutize_delegates():
    ctx = _ctx_for_helpers()
    assert ctx.absolutize("https://x.com/a/b", "../c") == "https://x.com/c"


def test_parse_money_delegates():
    from decimal import Decimal

    ctx = _ctx_for_helpers()
    assert ctx.parse_money("£51.77") == Decimal("51.77")


def test_clean_text_delegates():
    ctx = _ctx_for_helpers()
    assert ctx.clean_text("  hello   world\n") == "hello world"


# --------------------------------------------------------------------------- #
# Default per-host rate limiter is built from domain config inside GuardedClient
# --------------------------------------------------------------------------- #


async def test_fetch_builds_default_rate_limiter_from_domain_max_rps(fixture_server):
    """With no spy installed, GuardedClient lazily builds the host's limiter from
    its max_rps and still gates (a single fetch just succeeds; this proves no
    crash on the auto-built limiter path and that the registry gets populated)."""
    fixture_server.mode = "normal"
    cfg = _local_config(max_rps=100.0)
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        html = await ctx.fetch(f"{fixture_server.url}/catalogue/page-1.html")
    assert "product_pod" in html
    # the guard built and cached a per-host limiter at max_rps -> 0.01s interval
    assert ctx._guarded._limiters["127.0.0.1"]._min_interval == pytest.approx(0.01)
