"""Security tests for the central GuardedClient egress chokepoint (Milestone 4).

These pin the invariants from the M4 security review:

* **C1** — every HTTP GET (including each redirect hop) passes the allowlist
  gate, so a cross-host or SSRF redirect can never deliver an off-list body.
* **I1** — the recovery loop's GETs are rate-limited (they go through the guard).
* **I2** — each :class:`BackoffRetry` attempt is rate-limited (ditto).
* **I3** — each host gets its OWN min-interval limiter, never one shared gate.

They use ``respx`` to mock redirect chains and a tiny hand-built
:class:`AppConfig` authorizing only specific hosts. The fixture server already
covers the real-socket happy path; here we need to provoke exact redirect
statuses and cross-host hops, which respx does deterministically.
"""

import httpx
import pytest
import respx

from crawloop.access import (
    BackoffRetry,
    FetchError,
    GuardedClient,
    PlainHTTP,
    RateLimiter,
    RealFetchContext,
    build_http_client,
)
from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain
from crawloop.loop.access_recovery import recover_access


# --------------------------------------------------------------------------- #
# Test doubles / helpers
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


class CountingRateLimiter(RateLimiter):
    """A real :class:`RateLimiter` (no-op sleep) that counts every acquire."""

    def __init__(self, max_rps: float = 100.0):
        super().__init__(max_rps, sleep=self._noop)
        self.acquired: list[str] = []

    @staticmethod
    async def _noop(_seconds: float) -> None:
        return None

    async def acquire(self, domain: str) -> None:
        self.acquired.append(domain)
        await super().acquire(domain)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _config(domains: dict[str, float]) -> AppConfig:
    """Authorize each host in ``domains`` (host -> max_rps) with a plain ladder."""
    return AppConfig(
        respect_robots=False,
        domains={
            host: DomainConfig(
                domain=host,
                max_rps=rps,
                render_js=False,
                access_strategies=[("plain", {})],
            )
            for host, rps in domains.items()
        },
    )


def _guarded(client: httpx.AsyncClient, config: AppConfig) -> GuardedClient:
    return GuardedClient(client, config, env={})


# --------------------------------------------------------------------------- #
# C1 — per-hop allowlist gate on every redirect
# --------------------------------------------------------------------------- #


@respx.mock
async def test_cross_host_redirect_is_blocked():
    """good.com -> 302 -> evil.com. Only good.com is authorized, so the hop to
    evil.com raises UnauthorizedDomain and the "OFF" body is NEVER returned."""
    good = respx.get("https://good.com/p").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.com/x"})
    )
    evil = respx.get("https://evil.com/x").mock(
        return_value=httpx.Response(200, text="OFF")
    )
    cfg = _config({"good.com": 100.0})
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(UnauthorizedDomain):
            await ctx.fetch("https://good.com/p")
    assert good.called
    assert not evil.called  # off-list body was never even requested


@respx.mock
async def test_ssrf_redirect_to_localhost_is_blocked():
    """good.com -> 302 -> http://127.0.0.1/x. 127.0.0.1 is not authorized, so the
    SSRF redirect is blocked at the hop."""
    respx.get("https://good.com/p").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/x"})
    )
    internal = respx.get("http://127.0.0.1/x").mock(
        return_value=httpx.Response(200, text="SECRET")
    )
    cfg = _config({"good.com": 100.0})
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(
            cfg, store, client=client, browser_runner=FakeBrowserRunner()
        )
        with pytest.raises(UnauthorizedDomain):
            await ctx.fetch("https://good.com/p")
    assert not internal.called


@respx.mock
async def test_same_host_redirect_is_followed():
    """good.com/a -> 302 -> good.com/b -> 200. The per-hop check passes for both
    (same authorized host) so the redirect is followed and "OK" returned."""
    respx.get("https://good.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "https://good.com/b"})
    )
    respx.get("https://good.com/b").mock(
        return_value=httpx.Response(200, text="OK")
    )
    cfg = _config({"good.com": 100.0})
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        resp = await guarded.get("https://good.com/a")
    assert resp.status_code == 200
    assert resp.text == "OK"


@respx.mock
async def test_redirect_loop_is_capped_not_hung():
    """A redirect cycle within an authorized host raises FetchError (too many
    redirects) instead of looping forever."""
    respx.get("https://good.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "https://good.com/b"})
    )
    respx.get("https://good.com/b").mock(
        return_value=httpx.Response(302, headers={"location": "https://good.com/a"})
    )
    cfg = _config({"good.com": 100.0})
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        with pytest.raises(FetchError) as ei:
            await guarded.get("https://good.com/a")
    assert ei.value.status is None
    assert isinstance(ei.value.cause, RuntimeError)


# --------------------------------------------------------------------------- #
# I2 — BackoffRetry retries each go through the guard (rate-limited per attempt)
# --------------------------------------------------------------------------- #


@respx.mock
async def test_backoff_retry_rate_limits_every_attempt():
    """429-then-ok through a guarded PlainHTTP wrapped in BackoffRetry: the
    limiter is acquired once per GET attempt (>= number of attempts), not once
    for the whole BackoffRetry call."""
    route = respx.get("https://good.com/p")
    route.side_effect = [
        httpx.Response(429, text="slow"),
        httpx.Response(200, text="<html>ok</html>"),
    ]
    cfg = _config({"good.com": 100.0})
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        limiter = CountingRateLimiter()
        guarded._limiters["good.com"] = limiter  # spy on this host's limiter
        strat = BackoffRetry(PlainHTTP(guarded), tries=3, sleep=_noop_sleep)
        out = await strat.fetch("https://good.com/p")
    assert out.is_ok
    assert route.call_count == 2  # one 429, one ok
    assert len(limiter.acquired) >= 2  # acquired per attempt, not once total


# --------------------------------------------------------------------------- #
# I1 — recovery loop GETs are rate-limited (they go through the guard)
# --------------------------------------------------------------------------- #


@respx.mock
async def test_recovery_loop_rate_limits_every_get():
    """recover_access walking a multi-rung ladder against a perpetually-blocked
    host acquires the limiter at least once per GET it performs."""
    route = respx.get("https://good.com/p").mock(
        return_value=httpx.Response(403, text="<div class='cf-challenge'>no</div>")
    )
    # ladder = backoff(plain) + session; both are GET-based and will be blocked,
    # so the loop walks every rung every round -> several GETs.
    cfg = AppConfig(
        respect_robots=False,
        domains={
            "good.com": DomainConfig(
                domain="good.com",
                max_rps=100.0,
                render_js=False,
                access_strategies=[("backoff", {}), ("session", {})],
            )
        },
    )
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        limiter = CountingRateLimiter()
        guarded._limiters["good.com"] = limiter
        result = await recover_access(
            "https://good.com/p",
            config=cfg,
            access_store=store,
            guarded=guarded,
            browser_runner=FakeBrowserRunner(),
            env={},
            max_rounds=2,
        )
    assert result.ok is False
    gets = route.call_count
    assert gets >= 1
    assert len(limiter.acquired) >= gets  # every GET was rate-limited


# --------------------------------------------------------------------------- #
# I3 — per-host min-intervals are independent, never one shared gate
# --------------------------------------------------------------------------- #


async def test_per_host_limiters_have_distinct_intervals():
    """a.com (max_rps 10) and b.com (max_rps 0.5) get DISTINCT RateLimiter
    instances with distinct min-intervals (0.1 vs 2.0), not one shared limiter."""
    cfg = AppConfig(
        respect_robots=False,
        domains={
            "a.com": DomainConfig(domain="a.com", max_rps=10.0),
            "b.com": DomainConfig(domain="b.com", max_rps=0.5),
        },
    )
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        lim_a = guarded._limiter_for("a.com")
        lim_b = guarded._limiter_for("b.com")
    assert lim_a is not lim_b
    assert lim_a._min_interval == pytest.approx(0.1)
    assert lim_b._min_interval == pytest.approx(2.0)
    # same host -> same cached instance (registry, not rebuilt per call)
    assert guarded._limiter_for("a.com") is lim_a


# --------------------------------------------------------------------------- #
# I4 — production client defaults; guard forces non-redirect mode
# --------------------------------------------------------------------------- #


async def test_build_http_client_has_safe_defaults():
    """The production client factory sets non-redirect mode, an explicit timeout,
    and a bounded connection pool (I4)."""
    async with build_http_client() as client:
        assert client.follow_redirects is False
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 10.0


async def test_guarded_client_forces_non_redirect_mode_defensively():
    """Even if handed a follow-redirects client, GuardedClient flips it off so
    httpx can never silently chase a redirect past the per-hop allowlist gate."""
    cfg = _config({"good.com": 100.0})
    async with httpx.AsyncClient(follow_redirects=True) as client:
        GuardedClient(client, cfg, env={})
        assert client.follow_redirects is False
