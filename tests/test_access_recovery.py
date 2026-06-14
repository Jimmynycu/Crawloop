"""Tests for the access-recovery loop (Task 4.4).

``recover_access`` walks the ordered strategy ladder (saved winner first, then
every configured rung, de-duplicated by name) for up to ``max_rounds`` rounds,
persists the first strategy that gets through, and escalates the domain if none
do. These run against the real :class:`FixtureServer` in ``blocked`` mode with a
hand-built :class:`AppConfig` authorizing ``127.0.0.1``. Each rung's GET goes
through a shared :class:`GuardedClient` (the allowlist + rate-limit chokepoint),
so recovery is built with one. Every owned httpx client is closed via
``async with`` so none leak.
"""

import httpx
import pytest

from crawloop.access import GuardedClient
from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain
from crawloop.loop.access_recovery import RecoveryResult, recover_access


# --------------------------------------------------------------------------- #
# Test doubles
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


def _local_config(access_strategies: list[tuple[str, dict]]) -> AppConfig:
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=100.0,
        render_js=False,
        access_strategies=access_strategies,
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _guarded(client: httpx.AsyncClient, cfg: AppConfig) -> GuardedClient:
    """The shared allowlist + rate-limit chokepoint recovery builds every
    GET-based rung with."""
    return GuardedClient(client, cfg, env=_ENV)


# x-test-bypass=ok is the fixture's bypass header; env-inject the value.
_BYPASS_RUNG = ("bypass_token", {"header": "x-test-bypass", "value_env": "BYPASS"})
_ENV = {"BYPASS": "ok"}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_recover_finds_bypass_token_and_persists_it(fixture_server):
    fixture_server.mode = "blocked"
    cfg = _local_config([("backoff", {}), _BYPASS_RUNG])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
        )
    assert isinstance(result, RecoveryResult)
    assert result.ok is True
    assert result.strategy == "bypass_token"
    assert store.get_working_strategy("127.0.0.1") == "bypass_token"


async def test_recover_offlist_raises_unauthorized():
    cfg = _local_config([("backoff", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        with pytest.raises(UnauthorizedDomain):
            await recover_access(
                "https://evil.com/x",
                config=cfg, access_store=store, guarded=guarded,
                browser_runner=FakeBrowserRunner(), env=_ENV,
            )


async def test_recover_uses_saved_winner_first_round_one(fixture_server):
    """A pre-saved winner is tried first; it succeeds in round 1 and is the
    returned strategy. (Ordered so the saved rung is what clears the block.)"""
    fixture_server.mode = "blocked"
    cfg = _local_config([("backoff", {}), _BYPASS_RUNG])
    store = InMemoryAccessStore()
    store.set_working_strategy("127.0.0.1", "bypass_token")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
        )
    assert result.ok is True
    assert result.strategy == "bypass_token"
    assert result.rounds == 1  # saved winner is first in the ladder -> round 1


async def test_recover_all_fail_escalates(fixture_server):
    """Ladder = only backoff(plain), fixture stays blocked -> after max_rounds
    returns ok=False and the domain is marked escalated."""
    fixture_server.mode = "blocked"
    cfg = _local_config([("backoff", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV, max_rounds=2,
        )
    assert result.ok is False
    assert result.strategy is None
    assert result.rounds == 2
    assert store.statuses["127.0.0.1"] == "escalated"
    assert store.get_working_strategy("127.0.0.1") is None  # nothing persisted


async def test_recover_skips_unauthorized_captcha_then_bypass_wins(fixture_server):
    """A captcha rung with authorized=False raises NotEnabled -> it is SKIPPED
    (treated as unavailable), and a later bypass rung still wins overall."""
    fixture_server.mode = "blocked"
    cfg = _local_config(
        [("backoff", {}), ("captcha_solver", {"authorized": False}), _BYPASS_RUNG]
    )
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
        )
    assert result.ok is True
    assert result.strategy == "bypass_token"
    assert store.get_working_strategy("127.0.0.1") == "bypass_token"


async def test_recover_dedups_ladder_by_name(fixture_server):
    """Saved winner + the same kind configured must not be tried twice in a
    round: the ladder is de-duplicated by strategy name. Here the saved winner
    is backoff (which can't clear the block) and bypass is the only other rung;
    a duplicated backoff config entry must not change behavior."""
    fixture_server.mode = "blocked"
    cfg = _local_config([("backoff", {}), ("backoff", {}), _BYPASS_RUNG])
    store = InMemoryAccessStore()
    store.set_working_strategy("127.0.0.1", "backoff")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
        )
    # bypass still wins; the duplicate backoff rungs collapse to one.
    assert result.ok is True
    assert result.strategy == "bypass_token"


async def test_recover_writes_audit_event_on_success(fixture_server):
    """When an audit sink is provided, a success writes one event carrying the
    host and winning strategy."""
    fixture_server.mode = "blocked"
    cfg = _local_config([_BYPASS_RUNG])
    store = InMemoryAccessStore()
    events: list[dict] = []
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
            audit=events.append,
        )
    assert result.ok is True
    assert len(events) == 1
    assert events[0]["host"] == "127.0.0.1"
    assert events[0]["strategy"] == "bypass_token"


async def test_recover_browser_rung_succeeds_via_runner(fixture_server):
    """A stealth_browser rung clears the (HTTP-)blocked fixture because the
    browser runner is faked to return HTML — proving render strategies are
    walked too and recorded as the winner."""
    fixture_server.mode = "blocked"
    cfg = _local_config([("backoff", {}), ("stealth_browser", {})])
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        guarded = _guarded(client, cfg)
        result = await recover_access(
            f"{fixture_server.url}/catalogue/page-1.html",
            config=cfg, access_store=store, guarded=guarded,
            browser_runner=FakeBrowserRunner(), env=_ENV,
        )
    assert result.ok is True
    assert result.strategy == "stealth_browser"
    assert store.get_working_strategy("127.0.0.1") == "stealth_browser"
