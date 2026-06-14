"""Offline, deterministic tests for the real BrowserRunner (Wave 2).

NONE of these launch a real browser. They cover:

* :func:`crawloop.browser.nav_allowed` — the PURE allowlist predicate the
  route handler and the initial ``goto`` both consult. It must agree with
  :meth:`AppConfig.assert_authorized` on every host-spoof case (the same matrix
  ``tests/test_config.py`` pins), because the browser path bypasses
  :class:`GuardedClient` and is otherwise an allowlist hole.
* The None-runner guards: a render requested with no ``browser_runner`` wired
  must fail loudly, both at the strategy rung (:class:`BrowserFetch`) and at the
  context level (:meth:`RealFetchContext.fetch_rendered`).
* That a FAKE :class:`BrowserRunner` (canned HTML) flows through the browser /
  stealth rung and the context render path.
* That :class:`PlaywrightBrowserRunner` constructs WITHOUT launching a browser
  (imports are lazy) — skipped cleanly if playwright is not importable.

The live, browser-launching integration test lives in
``tests/test_browser_live.py`` and is gated behind ``RUN_BROWSER_TESTS=1``.
"""

import importlib.util

import httpx
import pytest

from crawloop.access import BrowserFetch, FetchError, RealFetchContext
from crawloop.browser import nav_allowed
from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain


def _config(*hosts: str) -> AppConfig:
    """Authorize each host in ``hosts`` with default policy."""
    if not hosts:
        hosts = ("books.toscrape.com",)
    return AppConfig(
        respect_robots=False,
        domains={h: DomainConfig(domain=h) for h in hosts},
    )


def _local_config(*, render_js: bool = False, access_strategies=None) -> AppConfig:
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=100.0,
        render_js=render_js,
        access_strategies=access_strategies if access_strategies is not None else [("backoff", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


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


class FakeBrowserRunner:
    """Canned-HTML :class:`BrowserRunner`, recording every call's args."""

    def __init__(self, html: str = "<html>rendered</html>"):
        self._html = html
        self.calls: list[dict] = []

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        self.calls.append(
            {"url": url, "stealth": stealth, "wait_for": wait_for, "extra_headers": extra_headers}
        )
        return self._html


# --------------------------------------------------------------------------- #
# nav_allowed — the pure allowlist predicate (mirrors test_config host spoofs)
# --------------------------------------------------------------------------- #


def test_nav_allowed_authorized_host_passes():
    cfg = _config("books.toscrape.com")
    assert nav_allowed("https://books.toscrape.com/p", cfg) is True


def test_nav_allowed_unlisted_host_refused():
    cfg = _config("books.toscrape.com")
    assert nav_allowed("https://evil.com/x", cfg) is False


def test_nav_allowed_strips_port():
    cfg = _config("books.toscrape.com")
    assert nav_allowed("https://books.toscrape.com:443/p", cfg) is True
    assert nav_allowed("https://evil.com:8080/p", cfg) is False


def test_nav_allowed_host_case_insensitive():
    cfg = _config("books.toscrape.com")
    assert nav_allowed("https://BOOKS.TOSCRAPE.COM/p", cfg) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://books.toscrape.com.evil.com/p",  # suffix spoof
        "https://evilbooks.toscrape.com/p",  # prefix spoof
        "https://sub.books.toscrape.com/p",  # subdomain (exact-host match only)
        "https://user:pass@evil.com/p",  # userinfo spoof: real host is evil.com
        "not a url",  # unparseable host
    ],
)
def test_nav_allowed_refuses_spoofs(url):
    """Same host-spoof matrix as ``test_config.test_assert_authorized_rejects_spoofs``;
    the browser predicate MUST refuse exactly what the HTTP gate refuses."""
    cfg = _config("books.toscrape.com")
    assert nav_allowed(url, cfg) is False


def test_nav_allowed_userinfo_at_evil_blocked_even_if_authorized_in_userinfo():
    """``https://books.toscrape.com@evil.com/`` parses to host evil.com (the part
    before ``@`` is userinfo), so it must be refused even though an authorized
    name appears before the ``@``."""
    cfg = _config("books.toscrape.com")
    assert nav_allowed("https://books.toscrape.com@evil.com/p", cfg) is False


def test_nav_allowed_agrees_with_assert_authorized():
    """nav_allowed is True exactly when assert_authorized does NOT raise — they
    are two faces of the same allowlist decision."""
    cfg = _config("books.toscrape.com")
    for url in (
        "https://books.toscrape.com/p",
        "https://books.toscrape.com:443/p",
        "https://evil.com/x",
        "https://books.toscrape.com@evil.com/p",
        "not a url",
    ):
        try:
            cfg.assert_authorized(url)
            raised = False
        except UnauthorizedDomain:
            raised = True
        assert nav_allowed(url, cfg) is (not raised)


# --------------------------------------------------------------------------- #
# None-runner guards — a render with no runner must fail loudly
# --------------------------------------------------------------------------- #


async def test_browser_fetch_with_no_runner_returns_error_outcome():
    """A :class:`BrowserFetch` built with ``runner=None`` must not crash with an
    AttributeError; it returns an ``error`` outcome carrying a clear message."""
    out = await BrowserFetch(None).fetch("https://books.toscrape.com/p")
    assert out.kind == "error"
    assert out.error is not None
    assert "no browser runner" in str(out.error).lower()


async def test_fetch_rendered_with_no_runner_raises_clearly():
    """Requesting a render on a context built with ``browser_runner=None`` raises
    a clear :class:`FetchError` rather than an AttributeError on ``None.render``."""
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(cfg, store, client=client, browser_runner=None)
        with pytest.raises(FetchError) as ei:
            await ctx.fetch_rendered("http://127.0.0.1/p")
    assert "no browser runner" in str(ei.value).lower()


async def test_fetch_rendered_with_no_runner_still_checks_allowlist_first():
    """The allowlist gate runs BEFORE the no-runner check: an off-list render with
    no runner raises UnauthorizedDomain (not the no-runner FetchError)."""
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(cfg, store, client=client, browser_runner=None)
        with pytest.raises(UnauthorizedDomain):
            await ctx.fetch_rendered("https://evil.com/x")


# --------------------------------------------------------------------------- #
# A fake runner flows through the browser/stealth rung + the context render path
# --------------------------------------------------------------------------- #


async def test_fake_runner_html_flows_through_browser_rung():
    runner = FakeBrowserRunner(html="<html>canned</html>")
    out = await BrowserFetch(runner, stealth=False).fetch("https://books.toscrape.com/p")
    assert out.is_ok
    assert out.html == "<html>canned</html>"
    assert runner.calls[0]["stealth"] is False


async def test_fake_runner_html_flows_through_stealth_rung():
    runner = FakeBrowserRunner(html="<html>stealthed</html>")
    out = await BrowserFetch(runner, stealth=True).fetch("https://books.toscrape.com/p")
    assert out.is_ok
    assert out.html == "<html>stealthed</html>"
    assert runner.calls[0]["stealth"] is True


async def test_fake_runner_flows_through_context_render(fixture_server):
    runner = FakeBrowserRunner(html="<html>ctx-rendered</html>")
    cfg = _local_config()
    store = InMemoryAccessStore()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = RealFetchContext(cfg, store, client=client, browser_runner=runner)
        html = await ctx.fetch_rendered(f"{fixture_server.url}/catalogue/page-1.html")
    assert html == "<html>ctx-rendered</html>"


# --------------------------------------------------------------------------- #
# PlaywrightBrowserRunner constructs without launching a browser (lazy import)
# --------------------------------------------------------------------------- #


_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not importable")
def test_playwright_runner_constructs_without_launching():
    """Constructing the runner must not import or launch a browser; it just holds
    config + options. (Imports are lazy, inside the async methods.)"""
    from crawloop.browser import PlaywrightBrowserRunner

    cfg = _config("books.toscrape.com")
    runner = PlaywrightBrowserRunner(cfg, headless=True, timeout_ms=5000)
    assert runner._config is cfg
    assert runner.engine == "playwright"
    # No browser/context created yet (lazy launch on first render).
    assert runner._browser is None
    assert runner._context is None


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not importable")
def test_stealth_runner_uses_patchright_engine():
    from crawloop.browser import PlaywrightBrowserRunner, StealthBrowserRunner

    cfg = _config("books.toscrape.com")
    runner = StealthBrowserRunner(cfg)
    assert isinstance(runner, PlaywrightBrowserRunner)
    assert runner.engine == "patchright"


@pytest.mark.skipif(not _HAS_PLAYWRIGHT, reason="playwright not importable")
async def test_playwright_runner_refuses_offlist_url_before_launch():
    """An off-list URL is refused by the up-front :func:`nav_allowed` check, which
    runs BEFORE any browser launch — so this raises without needing a browser."""
    from crawloop.browser import PlaywrightBrowserRunner

    cfg = _config("books.toscrape.com")
    runner = PlaywrightBrowserRunner(cfg)
    with pytest.raises(UnauthorizedDomain):
        await runner.render("https://evil.com/x", stealth=False)
    # still no browser launched (refused before launch)
    assert runner._browser is None
