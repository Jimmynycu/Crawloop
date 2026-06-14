"""Tests for the AccessStrategy ladder + ports (Task 4.2).

Every strategy returns a :class:`FetchOutcome`. These tests pin the full
status -> outcome mapping, exercise the real :class:`FixtureServer` for the
happy/blocked paths, and use ``respx`` to mock the specific HTTP status codes
that are awkward to provoke from a real server (429/401/500/404).

Every GET-based strategy now funnels through a :class:`GuardedClient` (the
single allowlist + rate-limit chokepoint), so these tests build one authorizing
the host under test. All httpx clients are created with ``async with`` so none
leak, and :class:`BackoffRetry` is driven with a no-op injected sleep so the
suite never actually waits.
"""

import httpx
import pytest
import respx
from parsel import Selector

from crawloop.access import (
    AccessStore,
    AccessStrategy,
    BackoffRetry,
    BrowserFetch,
    BrowserRunner,
    CaptchaSolver,
    FetchOutcome,
    GuardedClient,
    HeaderFetch,
    NotEnabled,
    PlainHTTP,
)
from crawloop.config import AppConfig, DomainConfig

# An off-fixture URL used for respx-mocked tests (respx intercepts by pattern).
MOCK_URL = "https://example.test/page"


def _guarded(client: httpx.AsyncClient, *hosts: str, max_rps: float = 100.0) -> GuardedClient:
    """A :class:`GuardedClient` authorizing ``hosts`` (default: the respx mock
    host and the fixture server host) so a strategy's GET passes the allowlist."""
    if not hosts:
        hosts = ("example.test", "127.0.0.1")
    cfg = AppConfig(
        respect_robots=False,
        domains={h: DomainConfig(domain=h, max_rps=max_rps) for h in hosts},
    )
    return GuardedClient(client, cfg, env={})


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    """In-memory :class:`BrowserRunner`: returns canned HTML and records the
    ``stealth`` flag / args it was called with."""

    def __init__(self, html: str = "<html>browser</html>", raises: Exception | None = None):
        self._html = html
        self._raises = raises
        self.calls: list[dict] = []

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        self.calls.append(
            {"url": url, "stealth": stealth, "wait_for": wait_for, "extra_headers": extra_headers}
        )
        if self._raises is not None:
            raise self._raises
        return self._html


class FlakyInner:
    """A fake :class:`AccessStrategy` that returns ``fail`` for the first
    ``fail_times`` calls, then ``ok`` forever after. Records its call count."""

    name = "flaky"

    def __init__(self, fail_times: int, fail: FetchOutcome | None = None):
        self._fail_times = fail_times
        self._fail = fail if fail is not None else FetchOutcome.err(RuntimeError("boom"))
        self.calls = 0

    async def fetch(self, url: str) -> FetchOutcome:
        self.calls += 1
        if self.calls <= self._fail_times:
            return self._fail
        return FetchOutcome.ok("<html>recovered</html>")


class InMemoryAccessStore:
    """An in-memory implementation of the :class:`AccessStore` port (the shape
    M5's registry will satisfy)."""

    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


# --------------------------------------------------------------------------- #
# PlainHTTP — against the real FixtureServer
# --------------------------------------------------------------------------- #


async def test_plain_http_normal_mode_returns_ok_with_books(fixture_server):
    fixture_server.mode = "normal"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        strat = PlainHTTP(_guarded(client))
        out = await strat.fetch(f"{fixture_server.url}/catalogue/page-1.html")

    assert out.is_ok
    assert out.status == 200
    # real parsed structure, not just a substring
    assert len(Selector(text=out.html).css("article.product_pod")) == 3


async def test_plain_http_blocked_mode_is_challenge(fixture_server):
    fixture_server.mode = "blocked"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        strat = PlainHTTP(_guarded(client))
        out = await strat.fetch(f"{fixture_server.url}/catalogue/page-1.html")

    assert out.kind == "blocked"
    assert out.status == 403
    assert out.marker == "challenge"


async def test_plain_http_has_default_name():
    async with httpx.AsyncClient(follow_redirects=False) as client:
        assert PlainHTTP(_guarded(client)).name == "plain"


# --------------------------------------------------------------------------- #
# PlainHTTP — status mapping via respx
# --------------------------------------------------------------------------- #


@respx.mock
async def test_plain_http_429_is_blocked_rate():
    respx.get(MOCK_URL).mock(return_value=httpx.Response(429, text="slow down"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "blocked"
    assert out.status == 429
    assert out.marker == "rate"


@respx.mock
async def test_plain_http_401_without_challenge_is_auth():
    respx.get(MOCK_URL).mock(return_value=httpx.Response(401, text="Login required"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "blocked"
    assert out.status == 401
    assert out.marker == "auth"


@respx.mock
async def test_plain_http_403_with_challenge_body_is_challenge():
    respx.get(MOCK_URL).mock(
        return_value=httpx.Response(403, text="<div class='cf-challenge'>nope</div>")
    )
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "blocked"
    assert out.marker == "challenge"


@respx.mock
async def test_plain_http_500_is_error():
    respx.get(MOCK_URL).mock(return_value=httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "error"
    assert out.status == 500


@respx.mock
async def test_plain_http_404_is_error_with_status():
    respx.get(MOCK_URL).mock(return_value=httpx.Response(404, text="gone"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "error"
    assert out.status == 404  # the M8 classifier reads .status to decide GONE


@respx.mock
async def test_plain_http_transport_error_is_error():
    respx.get(MOCK_URL).mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient(follow_redirects=False) as client:
        out = await PlainHTTP(_guarded(client)).fetch(MOCK_URL)
    assert out.kind == "error"
    assert isinstance(out.error, httpx.ConnectError)


# --------------------------------------------------------------------------- #
# HeaderFetch — same mapping as PlainHTTP, plus extra headers clear a block
# --------------------------------------------------------------------------- #


async def test_header_fetch_bypass_header_clears_block(fixture_server):
    fixture_server.mode = "blocked"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        strat = HeaderFetch(
            _guarded(client), headers={"x-test-bypass": "ok"}, name="bypass_token"
        )
        out = await strat.fetch(f"{fixture_server.url}/catalogue/page-1.html")

    assert out.is_ok
    assert out.status == 200
    assert len(Selector(text=out.html).css("article.product_pod")) == 3


async def test_header_fetch_without_header_is_blocked(fixture_server):
    fixture_server.mode = "blocked"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        strat = HeaderFetch(_guarded(client), headers={"x-irrelevant": "1"}, name="session")
        out = await strat.fetch(f"{fixture_server.url}/catalogue/page-1.html")

    assert out.kind == "blocked"
    assert out.marker == "challenge"


async def test_header_fetch_carries_its_name():
    async with httpx.AsyncClient(follow_redirects=False) as client:
        assert (
            HeaderFetch(_guarded(client), headers={"a": "b"}, name="bypass_token").name
            == "bypass_token"
        )


# --------------------------------------------------------------------------- #
# BackoffRetry — retries on error/rate, instant via injected no-op sleep
# --------------------------------------------------------------------------- #


async def _noop_sleep(_seconds: float) -> None:
    return None


async def test_backoff_retries_then_succeeds():
    inner = FlakyInner(fail_times=2)  # fail, fail, ok
    strat = BackoffRetry(inner, tries=3, sleep=_noop_sleep)
    out = await strat.fetch(MOCK_URL)
    assert out.is_ok
    assert inner.calls == 3  # two failures + the success


async def test_backoff_gives_up_after_tries_and_returns_last_error():
    inner = FlakyInner(fail_times=99)  # always fails
    strat = BackoffRetry(inner, tries=3, sleep=_noop_sleep)
    out = await strat.fetch(MOCK_URL)
    assert out.kind == "error"
    assert inner.calls == 3  # exactly `tries` attempts, no more


async def test_backoff_retries_on_rate_blocked():
    rate = FetchOutcome.blocked(429, "rate")
    inner = FlakyInner(fail_times=1, fail=rate)
    strat = BackoffRetry(inner, tries=3, sleep=_noop_sleep)
    out = await strat.fetch(MOCK_URL)
    assert out.is_ok
    assert inner.calls == 2  # retried the 429 once, then ok


async def test_backoff_does_not_retry_non_rate_block():
    """A challenge/auth block is not a transient rate issue: return immediately,
    let the recovery loop escalate to a different strategy instead."""
    auth = FetchOutcome.blocked(403, "auth")
    inner = FlakyInner(fail_times=1, fail=auth)
    strat = BackoffRetry(inner, tries=3, sleep=_noop_sleep)
    out = await strat.fetch(MOCK_URL)
    assert out.kind == "blocked"
    assert out.marker == "auth"
    assert inner.calls == 1  # no retry


async def test_backoff_sleeps_grow_exponentially():
    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    inner = FlakyInner(fail_times=99)
    strat = BackoffRetry(inner, tries=3, sleep=record_sleep, base=0.1)
    await strat.fetch(MOCK_URL)
    # sleeps between the 3 attempts: base*2**0, base*2**1 (no sleep after last)
    assert slept == [0.1, 0.2]


async def test_backoff_default_name():
    assert BackoffRetry(FlakyInner(0)).name == "backoff"


# --------------------------------------------------------------------------- #
# BrowserFetch — delegates to a BrowserRunner port
# --------------------------------------------------------------------------- #


async def test_browser_fetch_returns_runner_html():
    runner = FakeBrowserRunner(html="<html>rendered</html>")
    out = await BrowserFetch(runner).fetch(MOCK_URL)
    assert out.is_ok
    assert out.html == "<html>rendered</html>"


async def test_browser_fetch_runner_error_is_error():
    runner = FakeBrowserRunner(raises=RuntimeError("browser crashed"))
    out = await BrowserFetch(runner).fetch(MOCK_URL)
    assert out.kind == "error"
    assert isinstance(out.error, RuntimeError)


async def test_browser_fetch_non_stealth_name_and_flag():
    runner = FakeBrowserRunner()
    strat = BrowserFetch(runner, stealth=False)
    assert strat.name == "browser"
    await strat.fetch(MOCK_URL)
    assert runner.calls[0]["stealth"] is False


async def test_stealth_browser_fetch_name_and_flag_pass_through():
    runner = FakeBrowserRunner()
    strat = BrowserFetch(runner, stealth=True)
    assert strat.name == "stealth_browser"
    await strat.fetch(MOCK_URL)
    assert runner.calls[0]["stealth"] is True


# --------------------------------------------------------------------------- #
# CaptchaSolver — opt-in boundary, ships off
# --------------------------------------------------------------------------- #


async def test_captcha_solver_unauthorized_raises_not_enabled():
    with pytest.raises(NotEnabled):
        await CaptchaSolver(authorized=False).fetch(MOCK_URL)


async def test_captcha_solver_authorized_returns_not_implemented_error():
    out = await CaptchaSolver(authorized=True).fetch(MOCK_URL)
    assert out.kind == "error"
    assert isinstance(out.error, NotImplementedError)


async def test_captcha_solver_default_name():
    assert CaptchaSolver().name == "captcha_solver"


# --------------------------------------------------------------------------- #
# Ports — pin the protocol shapes for M5/M4b
# --------------------------------------------------------------------------- #


def test_in_memory_store_satisfies_access_store_protocol():
    store = InMemoryAccessStore()
    assert isinstance(store, AccessStore)
    store.set_working_strategy("d.com", "bypass_token")
    assert store.get_working_strategy("d.com") == "bypass_token"
    store.mark_domain_status("d.com", "escalated")
    assert store.statuses["d.com"] == "escalated"


def test_fake_browser_runner_satisfies_browser_runner_protocol():
    assert isinstance(FakeBrowserRunner(), BrowserRunner)


async def test_strategies_satisfy_access_strategy_protocol():
    # Every concrete strategy is a structural AccessStrategy.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client)
        assert isinstance(PlainHTTP(guarded), AccessStrategy)
        assert isinstance(HeaderFetch(guarded, headers={}, name="x"), AccessStrategy)
        assert isinstance(BackoffRetry(FlakyInner(0)), AccessStrategy)
        assert isinstance(BrowserFetch(FakeBrowserRunner()), AccessStrategy)
        assert isinstance(CaptchaSolver(), AccessStrategy)
