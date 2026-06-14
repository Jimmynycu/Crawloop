"""The real browser driver behind the :class:`~crawloop.access.BrowserRunner`
port — a Playwright/Patchright runner used by the ``browser`` / ``stealth_browser``
rungs of the access ladder.

SECURITY (browser-side of C1) — the single most important property of this file.

The HTTP path funnels every GET through :class:`~crawloop.access.GuardedClient`,
which allowlist-checks the initial URL *and every redirect hop* before the request
goes out. The browser path does NOT pass through that guard: a page can issue its
own top-level navigations (``location.href = ...``, ``<meta http-equiv=refresh>``,
a 30x served to the browser, a link the page auto-clicks). Without re-enforcement
that is the exact same hole as C1 — a page on an authorized host could bounce the
browser to ``evil.com`` and exfiltrate an off-list body.

So this runner re-enforces the allowlist itself, with three independent layers:

1. **Up-front gate.** Before any browser is launched, ``render`` calls
   :meth:`AppConfig.assert_authorized` on the target URL. An off-list URL raises
   :class:`~crawloop.config.UnauthorizedDomain` and never launches a browser.

2. **Per-navigation request interception.** Every page installs a
   ``page.route("**/*", ...)`` handler. For any request that is a **main-frame
   document navigation** (``request.is_navigation_request()`` and
   ``request.frame is page.main_frame``) the handler re-checks :func:`nav_allowed`
   on the request URL and **aborts** it if the host is not authorized. This fires
   on the initial goto AND on every in-page / JS / meta-refresh / 30x redirect the
   page tries to follow, so a page that JS-redirects to ``evil.com`` is aborted,
   not followed. Sub-resource requests (images/CSS/XHR) are allowed through
   untouched — only top-level document navigations are gated, which is what
   "where the browser ends up" means for an allowlist.

3. **Belt-and-suspenders frame check.** A ``framenavigated`` listener on the main
   frame double-checks the committed URL; if anything ever slips past the route
   handler and the main frame commits to an off-list host, ``render`` raises
   :class:`~crawloop.config.UnauthorizedDomain` rather than returning that
   page's content.

The allowlist decision lives in ONE pure function, :func:`nav_allowed`, used by
both the up-front gate and the route handler, and it delegates to
:meth:`AppConfig.assert_authorized` so it agrees with the HTTP gate on every
host-spoof case (userinfo ``@evil.com``, explicit port, suffix/prefix lookalikes,
subdomains, unparseable hosts).

Playwright / Patchright are imported lazily *inside* the async methods so importing
this module (and running the offline test suite) never requires a browser to be
installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawloop.config import AppConfig, UnauthorizedDomain

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime browser import
    from playwright.async_api import Browser, BrowserContext, Request


# A realistic desktop User-Agent (mirrors the HTTP path default). Targets are
# owned/authorized; this is courtesy, not evasion.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 30_000


def nav_allowed(url: str, config: AppConfig) -> bool:
    """Is the browser permitted to navigate to ``url``?

    The PURE, side-effect-free allowlist predicate consulted by BOTH the
    up-front gate in :meth:`PlaywrightBrowserRunner.render` and the per-request
    route handler. It delegates to :meth:`AppConfig.assert_authorized` (catching
    :class:`~crawloop.config.UnauthorizedDomain`) precisely so the browser
    decision is byte-for-byte the same as the HTTP gate's: host parsed by
    ``urlparse`` (so ``user:pass@evil.com`` resolves to ``evil.com``),
    lowercased, port stripped, exact-host match against the allowlist. Any URL
    whose host cannot be parsed or is not on the list returns ``False``.
    """
    try:
        config.assert_authorized(url)
    except UnauthorizedDomain:
        return False
    return True


class PlaywrightBrowserRunner:
    """A real :class:`~crawloop.access.BrowserRunner` backed by Playwright.

    Constructed with the :class:`AppConfig` (for the allowlist) plus options.
    The browser and a single context are launched lazily on the first
    :meth:`render` and reused across calls; :meth:`aclose` tears them down.

    :param config: the allowlist source; every navigation is checked against it.
    :param headless: run the browser headless (default ``True``).
    :param timeout_ms: navigation / wait timeout in milliseconds.
    :param engine: ``"playwright"`` (default) or ``"patchright"`` — selects which
        driver package is imported lazily. ``"patchright"`` is the stealth driver
        used by :class:`StealthBrowserRunner`.
    :param user_agent: the User-Agent the context sends.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        headless: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        engine: str = "playwright",
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if engine not in ("playwright", "patchright"):
            raise ValueError(f"unknown browser engine: {engine!r}")
        self._config = config
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.engine = engine
        self.user_agent = user_agent
        # Lazy: nothing is launched until the first render().
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # -- lifecycle --------------------------------------------------------- #

    async def _ensure_context(self) -> BrowserContext:
        """Launch the browser + context once, then reuse. Imports the chosen
        driver lazily so importing this module never needs a browser."""
        if self._context is not None:
            return self._context
        if self.engine == "patchright":
            from patchright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(user_agent=self.user_agent)
        self._context.set_default_timeout(self.timeout_ms)
        self._context.set_default_navigation_timeout(self.timeout_ms)
        return self._context

    async def aclose(self) -> None:
        """Close the context, browser, and Playwright driver (idempotent)."""
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> PlaywrightBrowserRunner:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- the BrowserRunner port -------------------------------------------- #

    async def render(
        self,
        url: str,
        *,
        stealth: bool = False,
        wait_for: str | None = None,
        extra_headers: dict | None = None,
    ) -> str:
        """Navigate to ``url`` and return the rendered HTML (``page.content()``).

        Enforces the allowlist on EVERY main-frame navigation (see module
        docstring): an off-list initial URL or any in-page redirect to an off-list
        host raises :class:`~crawloop.config.UnauthorizedDomain`.

        ``stealth`` is accepted for Protocol compatibility; the stealth driver is
        selected by the ``engine`` chosen at construction (see
        :class:`StealthBrowserRunner`), so this argument does not change behavior
        for an already-stealth runner. If ``wait_for`` (a CSS selector) is given,
        the call awaits that selector before returning. ``extra_headers`` are sent
        as additional HTTP headers for the navigation.
        """
        # LAYER 1 — up-front gate, BEFORE launching anything. An off-list target
        # never even starts a browser.
        self._config.assert_authorized(url)

        context = await self._ensure_context()
        page = await context.new_page()
        try:
            if extra_headers:
                await page.set_extra_http_headers(extra_headers)

            # LAYER 2 — abort any main-frame DOCUMENT navigation to an off-list
            # host. Fires on the initial goto AND every in-page/JS/meta/30x
            # redirect; sub-resources pass through untouched.
            async def _route_handler(route: object, request: Request) -> None:
                if (
                    request.is_navigation_request()
                    and request.frame is page.main_frame
                    and not nav_allowed(request.url, self._config)
                ):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", _route_handler)

            # LAYER 3 — record any off-list host the main frame actually commits
            # to, so a navigation that somehow slips the route handler still fails
            # the render instead of returning an off-list body.
            offlist_landing: list[str] = []

            def _on_framenavigated(frame: object) -> None:
                if frame is page.main_frame and not nav_allowed(frame.url, self._config):
                    offlist_landing.append(frame.url)

            page.on("framenavigated", _on_framenavigated)

            await page.goto(url, wait_until="domcontentloaded")
            if offlist_landing:
                raise UnauthorizedDomain(
                    f"browser navigation reached off-list host: {offlist_landing[-1]!r}"
                )
            if wait_for is not None:
                await page.wait_for_selector(wait_for)
            # Final guard: never return content from an off-list page.
            if not nav_allowed(page.url, self._config):
                raise UnauthorizedDomain(f"browser ended on off-list host: {page.url!r}")
            return await page.content()
        finally:
            await page.close()


class StealthBrowserRunner(PlaywrightBrowserRunner):
    """A :class:`PlaywrightBrowserRunner` that drives the stealth engine
    (Patchright) for the ``stealth_browser`` rung. Identical allowlist
    enforcement; only the underlying driver differs."""

    def __init__(
        self,
        config: AppConfig,
        *,
        headless: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        super().__init__(
            config,
            headless=headless,
            timeout_ms=timeout_ms,
            engine="patchright",
            user_agent=user_agent,
        )
