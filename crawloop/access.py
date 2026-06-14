"""Access layer: rate limiter, shared fetch result, ports, and the strategy ladder.

Generated crawlers never touch the network directly; they go through an injected
``FetchContext`` (built in M4b). Underneath that context sits this module: it
turns a request into HTML and, when a site blocks us (429 / 401 / 403 / a
Cloudflare-style challenge page), escalates through an ordered ladder of
:class:`AccessStrategy` implementations until one gets through. Everything here
only ever runs against allowlisted domains — the allowlist gate lives in config
and is enforced by ``FetchContext`` in M4b, not here.

This file defines, in order:

* :class:`FetchOutcome` — the uniform result every strategy returns.
* The port Protocols the rest of the layer depends on (:class:`AccessStrategy`,
  :class:`AccessStore`, :class:`BrowserRunner`) so M4b's recovery loop and M5's
  registry can be wired in without this module importing their concrete types.
* :class:`RateLimiter` — a per-domain minimum-interval async gate (Task 4.1).
* The concrete strategies (Task 4.2): :class:`PlainHTTP`, :class:`HeaderFetch`,
  :class:`BackoffRetry`, :class:`BrowserFetch`, :class:`CaptchaSolver`.
* The typed fetch errors (:class:`FetchBlocked`, :class:`FetchError`), the
  :func:`build_strategy` factory (config kind -> strategy), and
  :class:`RealFetchContext` — the concrete ``FetchContext`` injected into
  generated crawlers (Task 4.3). The factory is the single place that maps
  config strategy kinds to instances; both the context's fast path and M4b's
  recovery loop build strategies through it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

import httpx

from crawloop import contract
from crawloop.config import AppConfig

# Maximum redirect hops GuardedClient will follow before giving up. Each hop is
# allowlist-checked and rate-limited individually, so this also caps how long a
# malicious redirect cycle can spin before raising.
_MAX_REDIRECTS = 5

# A realistic desktop User-Agent so a plain GET does not look like a bot by
# default. Sites this POC targets are owned/authorized; this is courtesy, not
# evasion (evasion, if ever needed for an authorized domain, is an explicit
# opt-in strategy further down the ladder).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Substrings that mark a 401/403 body as an anti-bot challenge page rather than a
# plain authentication wall. Matched case-insensitively. Mirrors the markers the
# test FixtureServer emits in ``blocked`` mode.
_CHALLENGE_MARKERS = ("cf-challenge", "attention required")


# --------------------------------------------------------------------------- #
# Shared result type
# --------------------------------------------------------------------------- #


@dataclass
class FetchOutcome:
    """The single result type every :class:`AccessStrategy` returns.

    Exactly one of three kinds:

    * ``"ok"``      — got HTML. ``html`` is set, ``status`` is the 2xx code.
    * ``"blocked"`` — the site refused us. ``status`` is the HTTP code and
      ``marker`` says why: ``"rate"`` (429), ``"auth"`` (401/403 login wall), or
      ``"challenge"`` (401/403 anti-bot page). The recovery loop reads ``marker``
      to decide whether/how to escalate.
    * ``"error"``   — transport failure or a status we treat as terminal-for-this
      -strategy (5xx, or a non-auth 4xx like 404/410). ``error`` holds the
      exception if there was one; ``status`` holds the HTTP code if there was one.
    """

    kind: str  # "ok" | "blocked" | "error"
    html: str | None = None
    status: int | None = None
    marker: str | None = None  # for blocked: "rate" | "auth" | "challenge"
    error: Exception | None = None

    @classmethod
    def ok(cls, html: str, status: int = 200) -> FetchOutcome:
        return cls(kind="ok", html=html, status=status)

    @classmethod
    def blocked(cls, status: int, marker: str) -> FetchOutcome:
        return cls(kind="blocked", status=status, marker=marker)

    @classmethod
    def err(cls, error: Exception | None, status: int | None = None) -> FetchOutcome:
        return cls(kind="error", error=error, status=status)

    @property
    def is_ok(self) -> bool:
        return self.kind == "ok"


# --------------------------------------------------------------------------- #
# Ports (Protocols) — implemented by concrete types in later milestones
# --------------------------------------------------------------------------- #


@runtime_checkable
class AccessStrategy(Protocol):
    """One ordered attempt to turn a URL into HTML. Strategies are tried in
    sequence by the recovery loop; the first ``ok`` wins and its ``name`` is
    what gets persisted as the domain's working strategy."""

    name: str

    async def fetch(self, url: str) -> FetchOutcome: ...


@runtime_checkable
class AccessStore(Protocol):
    """Persistence port for "which strategy works for this domain".

    Defined here so the M4b recovery loop can record winners and reuse them
    without importing M5's concrete registry (which does not exist yet). M5's
    registry will implement this Protocol; tests use an in-memory fake.
    """

    def get_working_strategy(self, domain: str) -> str | None: ...

    def set_working_strategy(self, domain: str, strategy: str) -> None: ...

    def mark_domain_status(self, domain: str, status: str) -> None: ...


@runtime_checkable
class BrowserRunner(Protocol):
    """Port for a real browser driver (Playwright/Patchright, shipped later).

    :class:`BrowserFetch` delegates to this so unit tests can substitute a fake
    that returns canned HTML instead of launching a browser.

    SECURITY (browser-side of C1): the real implementation MUST re-check
    :meth:`AppConfig.assert_authorized` on every navigation and every in-page
    redirect it follows. Unlike the HTTP path, browser navigations do NOT pass
    through :class:`GuardedClient`, so a cross-host or SSRF redirect inside the
    page would otherwise escape the allowlist — the same hole as C1.
    """

    async def render(
        self,
        url: str,
        *,
        stealth: bool,
        wait_for: str | None = None,
        extra_headers: dict | None = None,
    ) -> str: ...


def _host(url: str) -> str:
    """Bare hostname of ``url`` (no port), used as the per-domain key."""
    return (urlparse(url).hostname or "").lower()


# --------------------------------------------------------------------------- #
# Task 4.1 — Per-domain async rate limiter
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Per-domain minimum-interval gate for outbound requests.

    ``acquire(domain)`` returns immediately the first time a domain is seen, then
    enforces a gap of ``1 / max_rps`` seconds between successive acquires for the
    SAME domain. Different domains are independent — each has its own lock and
    last-timestamp, so their acquires never block one another.

    Time is read from the asyncio event-loop clock by default (stable under test,
    immune to wall-clock jumps). ``sleep`` and ``time`` are injectable so tests
    can drive a fake clock and assert exact wait amounts without real waiting.
    """

    def __init__(
        self,
        max_rps: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        time: Callable[[], float] | None = None,
    ) -> None:
        if max_rps <= 0:
            raise ValueError(f"max_rps must be > 0, got {max_rps!r}")
        self._min_interval = 1.0 / max_rps
        self._sleep = sleep
        self._time = time
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def _now(self) -> float:
        if self._time is not None:
            return self._time()
        return asyncio.get_running_loop().time()

    def _lock_for(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[domain] = lock
        return lock

    async def acquire(self, domain: str) -> None:
        """Block until at least ``1 / max_rps`` seconds have passed since this
        domain's previous acquire, then record the new timestamp."""
        # Hold the per-domain lock across the wait so two coroutines hitting the
        # same domain serialize and each gets its own slot (no thundering herd).
        async with self._lock_for(domain):
            last = self._last.get(domain)
            if last is not None:
                wait = self._min_interval - (self._now() - last)
                if wait > 0:
                    await self._sleep(wait)
            self._last[domain] = self._now()


# --------------------------------------------------------------------------- #
# The single guarded egress chokepoint
# --------------------------------------------------------------------------- #


def build_http_client() -> httpx.AsyncClient:
    """The one place that constructs the production :class:`httpx.AsyncClient`.

    Codifies the I4 defaults so every wiring site (the engine in M5/M10, and the
    tests) gets the same safe client instead of hand-rolling one:

    * ``follow_redirects=False`` — :class:`GuardedClient` follows redirects
      itself so each hop is allowlist-checked; httpx must never chase one.
    * an explicit ``timeout`` — no request may hang forever.
    * a low connection ``limits`` cap — a single crawl target needs only a small
      pool; this bounds resource use.

    Callers own the returned client's lifecycle (``async with`` / ``aclose``).
    """
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


class GuardedClient:
    """The ONE gate every outbound HTTP GET in the access layer funnels through.

    The security model has two non-negotiable invariants and this class is where
    they are enforced so generated/strategy code can never bypass them:

    * **Allowlist (C1).** Every hop — the initial URL *and* each redirect target
      — is checked with :meth:`AppConfig.assert_authorized` *before* the request
      goes out. A cross-host or SSRF redirect to an off-list host raises
      :class:`~crawloop.config.UnauthorizedDomain` and is never fetched, so
      the engine "can only ever fetch allowlisted domains, nothing else, ever."
    * **Rate limit (I1/I2/I3).** Each hop acquires a PER-HOST
      :class:`RateLimiter` built from that host's ``max_rps`` and cached in a
      registry, so two hosts never share an interval and every retry / recovery
      attempt is throttled centrally rather than by individual strategies.

    Redirects are followed manually (the injected client runs in non-redirect
    mode) precisely so the per-hop checks above run on each ``Location``; httpx
    is never allowed to silently chase a redirect off the allowlist.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: AppConfig,
        *,
        env: Mapping[str, str] = os.environ,
    ) -> None:
        # Defensively force non-redirect mode: the guard does redirect-following
        # itself so each hop is allowlist-checked and rate-limited. If httpx
        # followed redirects, a 30x to an off-list host would slip the gate.
        client.follow_redirects = False
        self._client = client
        self._config = config
        self._env = env
        self._limiters: dict[str, RateLimiter] = {}

    def _limiter_for(self, host: str) -> RateLimiter:
        """The per-host limiter, built from the host's ``max_rps`` and cached.

        Each host gets its OWN :class:`RateLimiter` instance (its own interval
        and last-timestamp), so a slow host can never throttle a fast one and
        vice-versa.
        """
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = RateLimiter(self._config.domain_config(host).max_rps)
            self._limiters[host] = limiter
        return limiter

    async def get(self, url: str, *, headers: dict | None = None) -> httpx.Response:
        """GET ``url``, following redirects manually with a per-hop guard.

        At each hop: assert the host is authorized (raises and propagates on an
        off-list host), rate-limit on that host, then GET in non-redirect mode.
        A 30x with a ``Location`` resolves against the current URL and loops;
        anything else is returned. Exceeding :data:`_MAX_REDIRECTS` hops raises
        :class:`FetchError` rather than spinning on a redirect cycle.
        """
        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            host = _host(current_url)
            # Allowlist gate FIRST, every hop. Never caught here: an off-list URL
            # must fail loudly all the way out of fetch.
            self._config.assert_authorized(current_url)
            await self._limiter_for(host).acquire(host)
            resp = await self._client.get(current_url, headers=headers)
            location = resp.headers.get("location")
            if resp.is_redirect and location:
                current_url = urljoin(current_url, location)
                continue
            return resp
        raise FetchError(status=None, cause=RuntimeError("too many redirects"))


# --------------------------------------------------------------------------- #
# Task 4.2 — concrete strategies
# --------------------------------------------------------------------------- #


class NotEnabled(Exception):
    """Raised by an opt-in strategy that an operator has not explicitly enabled
    for this domain (currently only :class:`CaptchaSolver`)."""


def _is_challenge_body(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _map_response(resp: httpx.Response) -> FetchOutcome:
    """Map an HTTP response to a :class:`FetchOutcome`. Shared by every
    GET-based strategy so the status semantics live in exactly one place.

    * 2xx               -> ok(text, status)
    * 429               -> blocked(429, "rate")
    * 401 / 403         -> blocked(status, "challenge") if the body looks like an
                           anti-bot page, else blocked(status, "auth")
    * 5xx               -> err(None, status)
    * other 4xx (404..) -> err(None, status)  (M8 classifier reads .status)
    """
    status = resp.status_code
    if 200 <= status < 300:
        return FetchOutcome.ok(resp.text, status)
    if status == 429:
        return FetchOutcome.blocked(429, "rate")
    if status in (401, 403):
        marker = "challenge" if _is_challenge_body(resp.text) else "auth"
        return FetchOutcome.blocked(status, marker)
    # 5xx and every other 4xx are terminal for this strategy; the status is
    # preserved so the failure classifier can categorize (e.g. 404/410 -> GONE).
    return FetchOutcome.err(None, status)


class PlainHTTP:
    """Async ``GET`` via the shared :class:`GuardedClient`, mapping the response
    through :func:`_map_response`.

    Sends a realistic ``User-Agent`` by default. The GET goes through the
    injected :class:`GuardedClient`, so the per-hop allowlist gate and per-host
    rate limit apply to this strategy (and every redirect it triggers) without
    the strategy being able to skip them. ``headers`` are optional extra request
    headers merged on top of the default UA — :class:`HeaderFetch` reuses this
    same machinery with required headers and a custom name.
    """

    def __init__(
        self,
        guarded: GuardedClient,
        headers: dict | None = None,
        name: str = "plain",
    ) -> None:
        self.name = name
        self._guarded = guarded
        self._headers = {"User-Agent": DEFAULT_USER_AGENT, **(headers or {})}

    async def fetch(self, url: str) -> FetchOutcome:
        try:
            resp = await self._guarded.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            # Transport/timeout failures: no status, surface the exception.
            return FetchOutcome.err(exc)
        return _map_response(resp)


class HeaderFetch(PlainHTTP):
    """A :class:`PlainHTTP` that always sends an explicit set of extra headers.

    One strategy covers both "session auth" (an ``Authorization`` header / cookie
    from an injected credentials provider) and "owner WAF bypass token" (a shared
    secret header) — they are the same behavior ("GET with headers that clear a
    block"), differing only in which headers and which persisted ``name``. M4b's
    factory builds this twice with different ``headers``/``name`` rather than
    maintaining two near-identical classes.
    """

    def __init__(self, guarded: GuardedClient, *, headers: dict, name: str):
        super().__init__(guarded=guarded, headers=headers, name=name)


class BackoffRetry:
    """Wraps an inner strategy and retries transient failures with exponential
    backoff. Retries only on outcomes worth retrying with the SAME strategy:
    ``error`` (transport/5xx) and ``blocked(marker="rate")`` (429). A
    challenge/auth block is not transient — it returns immediately so the
    recovery loop can escalate to a *different* strategy.

    ``sleep`` is injectable (default :func:`asyncio.sleep`) so tests pass a no-op
    and never actually wait. ``tries`` is the total number of attempts.
    """

    def __init__(
        self,
        inner: AccessStrategy,
        tries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base: float = 0.1,
        name: str = "backoff",
    ) -> None:
        self.inner = inner
        self.tries = tries
        self._sleep = sleep
        self._base = base
        self.name = name

    @staticmethod
    def _should_retry(outcome: FetchOutcome) -> bool:
        if outcome.kind == "error":
            return True
        return outcome.kind == "blocked" and outcome.marker == "rate"

    async def fetch(self, url: str) -> FetchOutcome:
        outcome = await self.inner.fetch(url)
        for attempt in range(self.tries - 1):
            if not self._should_retry(outcome):
                return outcome
            await self._sleep(self._base * 2**attempt)
            # Each retry re-enters the inner strategy, which GETs through the
            # GuardedClient again, so every attempt is allowlist-checked and
            # rate-limited (I2) — the throttle is not consumed once per call.
            outcome = await self.inner.fetch(url)
        return outcome


class BrowserFetch:
    """Delegates rendering to a :class:`BrowserRunner` (the real driver is
    :class:`crawloop.browser.PlaywrightBrowserRunner`; a fake is used in
    tests). ``stealth`` selects the patched/stealth browser and is reflected in
    :attr:`name` (``"stealth_browser"`` vs ``"browser"``) and passed through to
    the runner. ``wait_for`` is an optional selector the runner waits on before
    returning, carried on the instance rather than the (now removed) per-call
    ``render`` flag. If ``runner`` is ``None`` (no browser wired), :meth:`fetch`
    returns an ``error`` outcome instead of crashing."""

    def __init__(
        self,
        runner: BrowserRunner | None,
        stealth: bool = False,
        wait_for: str | None = None,
    ) -> None:
        self.runner = runner
        self.stealth = stealth
        self.wait_for = wait_for
        self.name = "stealth_browser" if stealth else "browser"

    async def fetch(self, url: str) -> FetchOutcome:
        if self.runner is None:
            # No browser driver wired (e.g. an offline build). Surface a clear
            # error outcome instead of an AttributeError on ``None.render`` so the
            # recovery loop treats this rung as failed and moves on.
            return FetchOutcome.err(
                RuntimeError("no browser runner configured for the browser strategy")
            )
        try:
            html = await self.runner.render(
                url, stealth=self.stealth, wait_for=self.wait_for
            )
        except Exception as exc:  # noqa: BLE001 — any browser failure -> error outcome
            return FetchOutcome.err(exc)
        return FetchOutcome.ok(html)


class CaptchaSolver:
    """The opt-in boundary for captcha solving. Intentionally a stub.

    The system never auto-defeats captchas. ``fetch`` raises :class:`NotEnabled`
    unless an operator has explicitly set ``authorized=True`` for the domain;
    even then, since no captcha provider ships in this POC, it returns an
    ``error`` outcome wrapping ``NotImplementedError``. Wiring a real provider is
    a deliberate, per-domain operator decision — not a default capability.
    """

    def __init__(self, authorized: bool = False, name: str = "captcha_solver") -> None:
        self.authorized = authorized
        self.name = name

    async def fetch(self, url: str) -> FetchOutcome:
        if not self.authorized:
            raise NotEnabled(
                "captcha solving is not enabled for this domain; an operator must "
                "explicitly authorize it and configure a provider"
            )
        return FetchOutcome.err(NotImplementedError("no captcha provider configured"))


# --------------------------------------------------------------------------- #
# Task 4.3 — typed fetch errors, strategy factory, RealFetchContext
# --------------------------------------------------------------------------- #


class FetchBlocked(Exception):
    """Raised by :class:`RealFetchContext` when the chosen strategy was actively
    refused (a ``blocked`` outcome). Carries the HTTP ``status`` and the
    ``marker`` (``"rate"`` / ``"auth"`` / ``"challenge"``) so the M8 failure
    classifier and the recovery loop can decide how to escalate."""

    def __init__(self, status: int | None, marker: str | None) -> None:
        self.status = status
        self.marker = marker
        super().__init__(f"blocked (status={status}, marker={marker!r})")


class FetchError(Exception):
    """Raised by :class:`RealFetchContext` for a terminal-for-this-strategy
    failure (transport error or a status like 5xx / 404). Carries the HTTP
    ``status`` (if any) and the underlying ``cause`` exception (if any) so the
    M8 classifier can categorize (e.g. 404/410 -> GONE)."""

    def __init__(self, status: int | None, cause: Exception | None) -> None:
        self.status = status
        self.cause = cause
        super().__init__(f"fetch error (status={status}, cause={cause!r})")


def _session_headers(params: dict, env: Mapping[str, str]) -> dict:
    """Build the headers for a ``session`` strategy from an env-stored credential.

    POC choice: the credential is injected as a **Cookie** header
    (``{"cookie": value}``) — a session cookie is the most natural artifact of a
    real login and needs no scheme prefix. There is no live login flow in the
    POC; a missing ``creds_env`` (or missing env var) yields an empty value, so
    the strategy still constructs but simply won't clear a block.
    """
    creds_env = params.get("creds_env")
    value = env.get(creds_env, "") if creds_env else ""
    return {"cookie": value}


def build_strategy(
    kind: str,
    params: dict,
    *,
    guarded: GuardedClient,
    browser_runner: BrowserRunner | None,
    env: Mapping[str, str] = os.environ,
) -> AccessStrategy:
    """Map a config ``(kind, params)`` to a concrete :class:`AccessStrategy`.

    The single source of truth for "config kind -> strategy instance". Both
    :class:`RealFetchContext` (its inline fast path) and the M4b recovery loop
    build every strategy through here, so the mapping lives in exactly one place.
    Every GET-based strategy is built with the shared :class:`GuardedClient`, so
    no strategy can reach the network without passing the per-hop allowlist gate
    and per-host rate limit.

    Credentials/secrets are read from ``env`` (no live login/token exchange in
    the POC). Unknown kinds raise :class:`ValueError`.
    """
    if kind == "plain":
        return PlainHTTP(guarded)
    if kind == "backoff":
        return BackoffRetry(PlainHTTP(guarded))
    if kind == "browser":
        return BrowserFetch(browser_runner, stealth=False)
    if kind == "stealth_browser":
        return BrowserFetch(browser_runner, stealth=True)
    if kind == "session":
        return HeaderFetch(guarded, headers=_session_headers(params, env), name="session")
    if kind == "bypass_token":
        header = params["header"]
        value = env.get(params.get("value_env", ""), "")
        return HeaderFetch(guarded, headers={header: value}, name="bypass_token")
    if kind == "captcha_solver":
        return CaptchaSolver(authorized=bool(params.get("authorized", False)))
    raise ValueError(f"unknown access strategy kind: {kind!r}")


class RealFetchContext:
    """The concrete :class:`~crawloop.contract.FetchContext` injected into
    generated crawlers.

    Every fetch (1) passes the hard allowlist gate and (2) runs **one** strategy
    — the saved working strategy for the domain if there is one, else the
    domain's fast default. The allowlist gate and per-host rate limit now live in
    the shared :class:`GuardedClient` that every GET-based strategy is built
    with, so they apply on every hop and every retry automatically. The inline
    path deliberately does NOT walk the whole strategy ladder: escalation through
    the ladder on a block is the recovery loop's job (M4b Task 4.4). On a
    ``blocked`` outcome it raises :class:`FetchBlocked`; on an ``error`` outcome,
    :class:`FetchError`. :class:`~crawloop.config.UnauthorizedDomain` from
    the allowlist gate is never caught — an off-list URL (including one reached
    via redirect) must fail loudly.

    The coercion helpers (:meth:`absolutize`/:meth:`parse_money`/
    :meth:`clean_text`) delegate to the shared :mod:`crawloop.contract`
    module so there is one implementation of each.
    """

    def __init__(
        self,
        config: AppConfig,
        access_store: AccessStore,
        *,
        client: httpx.AsyncClient,
        browser_runner: BrowserRunner | None,
        env: Mapping[str, str] = os.environ,
    ) -> None:
        self._config = config
        self._store = access_store
        self._browser_runner = browser_runner
        self._env = env
        # ONE shared guard for this context: every GET-based strategy it builds
        # funnels through here, so the allowlist gate and per-host rate limit are
        # applied on every hop and every retry without the strategy being able to
        # bypass them.
        self._guarded = GuardedClient(client, config, env=env)

    @property
    def guarded(self) -> GuardedClient:
        """The ONE :class:`GuardedClient` this context fetches through.

        Exposed read-only so the engine's access-recovery path can borrow the
        SAME guard (and therefore the same per-host :class:`RateLimiter` cache)
        instead of constructing a second one — central per-domain rate limiting
        (design §6 / I2). Recovery and the inline fast path must share one guard
        so a single host is never hit at ~2x its ``max_rps`` when they interleave.
        """
        return self._guarded

    # -- internals --------------------------------------------------------- #

    def _build(self, kind: str, params: dict) -> AccessStrategy:
        return build_strategy(
            kind,
            params,
            guarded=self._guarded,
            browser_runner=self._browser_runner,
            env=self._env,
        )

    def _wants_stealth(self, host: str) -> bool:
        kinds = {k for k, _ in self._config.domain_config(host).access_strategies}
        return "stealth_browser" in kinds

    def _fast_strategy(self, host: str) -> AccessStrategy:
        """Pick the single strategy the inline fetch will try.

        Saved working strategy wins (with its configured params if known). Else,
        for a JS-rendered domain whose first configured rung isn't a browser one,
        prefer a browser strategy as the HTTP default; otherwise build from the
        first configured rung (which defaults to ``backoff`` per DomainConfig).
        """
        dc = self._config.domain_config(host)
        saved = self._store.get_working_strategy(host)
        if saved is not None:
            return self._build(saved, self._params_for(host, saved))

        first_kind, first_params = dc.access_strategies[0]
        if dc.render_js and first_kind not in ("browser", "stealth_browser"):
            return self._build(
                "stealth_browser" if self._wants_stealth(host) else "browser", {}
            )
        return self._build(first_kind, first_params)

    def _params_for(self, host: str, kind: str) -> dict:
        """Configured params for ``kind`` on ``host`` (first match), else ``{}``.

        Lets a saved winner like ``bypass_token`` be rebuilt with its header /
        env mapping instead of an empty param dict.
        """
        for k, params in self._config.domain_config(host).access_strategies:
            if k == kind:
                return params
        return {}

    @staticmethod
    def _unwrap(outcome: FetchOutcome) -> str:
        """Turn a fetch outcome into HTML, or raise the typed error for it."""
        if outcome.is_ok:
            assert outcome.html is not None  # ok always carries html
            return outcome.html
        if outcome.kind == "blocked":
            raise FetchBlocked(outcome.status, outcome.marker)
        raise FetchError(outcome.status, outcome.error)

    # -- FetchContext API -------------------------------------------------- #

    async def fetch(self, url: str) -> str:
        # Allowlist gate up front (defense in depth — GuardedClient re-checks the
        # first hop and every redirect). The per-host rate limit is applied
        # inside GuardedClient.get, once per hop.
        self._config.assert_authorized(url)  # off-list -> UnauthorizedDomain (uncaught)
        host = _host(url)
        strategy = self._fast_strategy(host)
        return self._unwrap(await strategy.fetch(url))

    async def fetch_rendered(self, url: str, wait_for: str | None = None) -> str:
        # The browser path bypasses GuardedClient, so the real
        # ``PlaywrightBrowserRunner`` re-checks ``config.assert_authorized`` on
        # every navigation AND every in-page redirect (browser-side of C1). The
        # up-front check here covers the initial URL as defense in depth and is
        # what refuses an off-list render even when no runner is wired.
        self._config.assert_authorized(url)  # off-list -> UnauthorizedDomain (uncaught)
        host = _host(url)
        html = await self._render(url, host, wait_for)
        return html

    async def _render(self, url: str, host: str, wait_for: str | None) -> str:
        """Force a browser render, honoring the domain's stealth preference."""
        if self._browser_runner is None:
            # A render was explicitly requested but no browser driver is wired.
            # Fail loudly (after the allowlist gate above) rather than crashing on
            # ``None.render`` or silently returning nothing.
            raise FetchError(
                None,
                RuntimeError(
                    "no browser runner configured; cannot render "
                    f"{url!r} (wire a BrowserRunner to enable rendering)"
                ),
            )
        try:
            return await self._browser_runner.render(
                url, stealth=self._wants_stealth(host), wait_for=wait_for
            )
        except Exception as exc:  # noqa: BLE001 — any browser failure -> FetchError
            raise FetchError(None, exc) from exc

    # -- coercion helpers delegate to the contract module ------------------ #

    def absolutize(self, base: str, href: str | None) -> str | None:
        return contract.absolutize(base, href)

    def parse_money(self, raw):
        return contract.parse_money(raw)

    def clean_text(self, raw: str | None) -> str | None:
        return contract.clean_text(raw)
