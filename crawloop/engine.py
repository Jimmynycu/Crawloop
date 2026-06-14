"""The engine: the ``request()`` orchestration that wires the §8 runtime flow.

This is the front door. Everything earlier in the system — the allowlist, the
router, the registry version ladder, the validator, the failure classifier, the
T2 fallback, the access-recovery ladder, the regeneration Loop — is a piece that
:class:`Engine` composes into one decision tree. A caller hands :meth:`request`
a URL (and, for a never-seen site, a target schema) and the engine returns the
extracted items plus *how* it got them.

The flow, per design §8:

1. **Authorize.** ``config.assert_authorized(url)`` first, always. An off-list
   URL raises :class:`~crawloop.config.UnauthorizedDomain` straight out — a
   hard policy stop that is NEVER routed into healing or recovery. (The classifier
   re-raises it too, so even a redirect that lands off-list mid-fetch propagates
   rather than being "recovered" onto an unauthorized host.)
2. **Route.** :func:`~crawloop.router.route` finds the registered family
   whose ``url_patterns`` match. A hit is the fast path; a miss is bootstrap.
3. **Known family** — run the registry ladder (:func:`~crawloop.executor.\
   run_family`). On success serve it (no LLM). On failure, classify
   (:func:`~crawloop.loop.trigger.classify`) and act:
   * **DRIFT** — serve NOW via T2 (:func:`~crawloop.fallback.direct_extract`)
     and trigger the regeneration Loop (inline or scheduled) once.
   * **BLOCKED_\*** — escalate the *access* ladder via
     :func:`~crawloop.loop.access_recovery.recover_access` (NOT the healing
     Loop), then retry the family ONCE; serve if it clears, else escalate.
   * **TRANSIENT** — retry the family up to ``max_transient_retries`` with a short
     (injected, so tests are instant) backoff; give up empty when exhausted.
   * **GONE** — the page is permanently gone; return empty, do NOT regenerate.
4. **Unknown family** — bootstrap: REQUIRE a ``schema``, serve NOW via T2, then
   register a family (a derived URL pattern + the schema) and trigger the Loop to
   grow a first crawler.

The loop is bounded everywhere: recovery retries the family once, transient is
capped, drift triggers the Loop exactly once. There is no monitor, no background
worker pool, and no T1 fingerprinting here — "serve now, heal in the background"
is the whole contract (YAGNI for the POC).

ONE :class:`~crawloop.access.RealFetchContext` is built per engine and shared
across every request (the registry is the context's ``AccessStore``). The context
owns the ONE :class:`~crawloop.access.GuardedClient`; the recovery path
borrows it (``ctx.guarded``) so per-host rate limiting stays central rather than
split across two guards.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from crawloop import validator
from crawloop.access import FetchBlocked, FetchError, RealFetchContext
from crawloop.config import AppConfig, UnauthorizedDomain
from crawloop.executor import run_family
from crawloop.fallback import ExtractionFailed, direct_extract
from crawloop.hybrid import fill_residual, merge_record
from crawloop.llm import Completer
from crawloop.loop.access_recovery import recover_access
from crawloop.loop.driver import LoopResult, run_loop
from crawloop.loop.trigger import FailureClass, classify
from crawloop.registry import Registry
from crawloop.router import route


class EngineError(Exception):
    """Raised by :meth:`Engine.request` for a caller/usage error the engine
    cannot serve — currently only "an unknown family was requested without a
    schema to bootstrap from". Distinct from the recoverable failure classes
    (which the engine handles internally and reports via ``RequestResult.reason``)
    and from :class:`~crawloop.config.UnauthorizedDomain` (a hard policy stop
    that propagates as itself)."""


@dataclass
class RequestResult:
    """The outcome of one :meth:`Engine.request`.

    * ``items`` — the extracted records (possibly empty on a GONE / escalated /
      exhausted outcome).
    * ``source`` — HOW the items were produced: ``"registry"`` (the version
      ladder served them), ``"fallback"`` (T2 direct extraction after drift),
      ``"recovered"`` (the ladder served them after access recovery, or an empty
      escalation), or ``"bootstrap"`` (T2 served a never-seen family).
    * ``family`` — the family that handled the URL (``None`` only if routing
      found none AND bootstrap did not derive one).
    * ``used_version`` — the ladder rung that served (registry/recovered paths),
      else ``None``.
    * ``loop`` — the :class:`LoopResult` when the regeneration Loop ran inline,
      else ``None`` (scheduled in the background, or not triggered).
    * ``recovered_strategy`` — the winning access strategy when recovery ran,
      else ``None``.
    * ``reason`` — a short human label for the outcome ("ok", "drift->fallback",
      "blocked: escalated", "transient: gave up", "gone", "new family", ...).
    """

    items: list[dict]
    source: str
    family: str | None
    used_version: int | None = None
    loop: LoopResult | None = None
    recovered_strategy: str | None = None
    reason: str = ""
    # True when the hybrid tail-fill ran on the registry fast path: the deterministic
    # crawler served the items and ONE small LLM call filled the family's residual
    # fields (merged in, deterministic values kept). False whenever the hybrid was
    # skipped — disabled, offline, no residual set, or this was not a fast-path serve.
    hybrid_filled: bool = False


class Engine:
    """Composes the §8 runtime flow behind a single :meth:`request` coroutine.

    Construct it once with the shared infrastructure (config, registry, completer,
    an httpx client, a browser runner, and the dirs the Loop needs); it builds the
    one :class:`RealFetchContext` every request reuses. That context owns the
    single :class:`GuardedClient`, which the access-recovery path borrows (via
    ``self._ctx.guarded``) rather than constructing a second one — so per-host
    rate limiting is central (one :class:`RateLimiter` cache per host), not split
    across two guards (I2). The registry doubles as the context's ``AccessStore``
    (it implements that Protocol), so a strategy recovery wins is persisted there
    and reused on the next fetch automatically.
    """

    def __init__(
        self,
        config: AppConfig,
        registry: Registry,
        completer: Completer,
        *,
        client: httpx.AsyncClient,
        browser_runner: object,
        fixtures_dir: Path,
        snapshots_dir: Path | None = None,
        model: str = "anthropic/claude-fable-5",
        run_loop_inline: bool = True,
        max_transient_retries: int = 2,
        hybrid: bool = True,
        offline: bool = False,
        env: Mapping[str, str] = os.environ,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: str | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._completer = completer
        self._browser_runner = browser_runner
        self._fixtures_dir = Path(fixtures_dir)
        self._snapshots_dir = Path(snapshots_dir) if snapshots_dir is not None else None
        self._model = model
        self._run_loop_inline = run_loop_inline
        self._max_transient_retries = max_transient_retries
        # Hybrid tail-fill: after the deterministic crawler serves the fast path,
        # fill the family's known residual fields with ONE small LLM call (see
        # _maybe_hybrid_fill). ``hybrid=False`` disables it entirely; ``offline``
        # (the CLI's --offline) also disables it, since offline runs have no model
        # to call — both keep the fast path deterministic-only with ZERO LLM calls.
        self._hybrid = hybrid
        self._offline = offline
        self._env = env
        self._sleep = sleep
        self._now = now
        # ONE shared fetch context (registry is the AccessStore), reused across
        # every request. The context owns the SINGLE GuardedClient — recovery
        # borrows it via ``self._ctx.guarded`` so per-host rate limiting is
        # central (one RateLimiter cache per host), not split across two guards
        # that would let a single host be hit at ~2x its max_rps (I2).
        self._ctx = RealFetchContext(
            config, registry, client=client, browser_runner=browser_runner, env=env
        )
        # Hold references to fire-and-forget loop tasks so they are not GC'd
        # mid-flight; a done-callback retrieves any exception (the background
        # Loop is best-effort — its failure must never surface as an unhandled
        # task error in the serving path).
        self._bg_tasks: set[asyncio.Task] = set()
        # Per-family in-flight guard (§8: "one in-flight job per family"). In
        # background mode two concurrent drift requests for the same family would
        # otherwise each spawn a full run_loop -> double promotion + double LLM
        # spend; this set dedups the BACKGROUND branch (inline is naturally
        # serialized). A family is added when its loop is scheduled and removed
        # in the task's done-callback.
        self._inflight_families: set[str] = set()

    # -- public API -------------------------------------------------------- #

    async def request(self, url: str, *, schema: str | None = None) -> RequestResult:
        """Serve ``url`` per design §8 (see the module docstring for the flow).

        ``schema`` is required only on the bootstrap path (an unknown family);
        for a known family the family's stored ``schema_ref`` is used and
        ``schema`` is ignored. Raises :class:`~crawloop.config.\
        UnauthorizedDomain` for an off-list URL and :class:`EngineError` for an
        unknown family with no schema to bootstrap from.
        """
        # 1) Hard policy stop FIRST — never routed into healing/recovery.
        self._config.assert_authorized(url)

        # 2) Route to a known family, or fall to bootstrap.
        family = route(url, self._registry)
        if family is None:
            return await self._bootstrap(url, schema)
        return await self._serve_known_family(url, family)

    # -- known-family path ------------------------------------------------- #

    async def _serve_known_family(self, url: str, family: str) -> RequestResult:
        """Run the registry ladder for ``family``; on failure classify + act."""
        schema_ref = self._registry.get_family(family)["schema_ref"]
        try:
            run = await self._run_family(url, family)
        except Exception as exc:  # noqa: BLE001 — classified below
            # classify() RE-RAISES UnauthorizedDomain (a redirect off-list mid
            # fetch must propagate, never be "recovered"); every other failure
            # gets a recovery class.
            cls = classify(exc)
            return await self._handle_failure(url, family, schema_ref, exc, cls)
        # The deterministic crawler served (free, no LLM). On a wide schema it may
        # systematically leave a few residual fields blank; the hybrid fills ONLY
        # those, for this family, with one small LLM call (or none — see below).
        items, hybrid_filled = await self._maybe_hybrid_fill(
            url, family, schema_ref, run.items
        )
        return RequestResult(
            items=items,
            source="registry",
            family=family,
            used_version=run.used_version,
            reason="ok",
            hybrid_filled=hybrid_filled,
        )

    async def _maybe_hybrid_fill(
        self, url: str, family: str, schema_ref: str, items: list[dict]
    ) -> tuple[list[dict], bool]:
        """Tail-fill the family's residual fields into ``items``; return ``(items, ran)``.

        The runtime half of the deterministic-core + LLM-tail hybrid. It runs at MOST
        ONE small LLM call per page, and ONLY when ALL of:

        * the hybrid is enabled (``hybrid=True``) and we are not ``offline`` — offline
          has no model to call, so it stays deterministic-only;
        * the family's active version has a non-empty residual set
          (:meth:`Registry.active_residual_fields` — ``[]`` for a complete crawler or a
          legacy/pre-hybrid version), so when there is nothing to fill the cost is ``$0``;
        * there are items to fill.

        When it runs it re-fetches the page once, makes the single targeted
        :func:`~crawloop.hybrid.fill_residual` call for just the residual fields,
        and merges the result into EACH item (:func:`~crawloop.hybrid.merge_record`
        — deterministic values always win), then records a cheap ``hybrid_fill`` audit
        signal. Wholly best-effort: a re-fetch failure (or any non-policy error) skips
        the fill and returns the deterministic items unchanged — the fast path must
        never be broken by the tail-fill. ``UnauthorizedDomain`` still propagates (an
        off-list redirect mid re-fetch is a hard policy stop).

        Returns ``(items, ran)``: ``ran`` is True only when the LLM tail-fill actually
        executed (so the caller can surface it on the result + the audit reflects it).
        """
        if not self._hybrid or self._offline or not items:
            return items, False
        residual_fields = self._registry.active_residual_fields(family)
        if not residual_fields:
            return items, False  # nothing to fill -> no LLM call ($0).

        try:
            html = await self._ctx.fetch(url)
        except UnauthorizedDomain:
            raise
        except (FetchBlocked, FetchError):
            # Can't re-fetch the page to tail-fill: keep the deterministic items as-is
            # (they already served fine). The tail-fill is an enhancement, not a gate.
            return items, False

        tail = await fill_residual(
            html, residual_fields, schema_ref, self._completer, model=self._model
        )
        if not tail:
            # The one call produced nothing usable (or the residual fields weren't on
            # this page). Items stand; we don't mark a fill since none happened.
            return items, False

        merged = [merge_record(item, tail) for item in items]
        # Cheap audit signal that a hybrid tail-fill happened on this family/page.
        self._registry.write_audit(
            "hybrid_fill",
            family=family,
            data={"url": url, "residual_fields": residual_fields, "filled": list(tail)},
            now=self._now,
        )
        return merged, True

    async def _handle_failure(
        self,
        url: str,
        family: str,
        schema_ref: str,
        exc: Exception,
        cls: FailureClass,
    ) -> RequestResult:
        """Dispatch a classified known-family failure to its §8 response."""
        if cls is FailureClass.DRIFT:
            return await self._serve_drift(url, family, schema_ref)
        if cls in (
            FailureClass.BLOCKED_RATE,
            FailureClass.BLOCKED_AUTH,
            FailureClass.BLOCKED_CHALLENGE,
        ):
            return await self._serve_blocked(url, family)
        if cls is FailureClass.TRANSIENT:
            return await self._serve_transient(url, family)
        # GONE: the page is permanently gone — return empty, never regenerate.
        return RequestResult(
            items=[], source="registry", family=family, reason="gone"
        )

    async def _serve_drift(
        self, url: str, family: str, schema_ref: str
    ) -> RequestResult:
        """DRIFT: serve NOW via T2, then trigger the regeneration Loop once.

        The page changed shape so the ladder could not parse it, but T2 (a model
        reading the raw HTML against the schema) usually still can — so the caller
        gets fresh items immediately. Then the Loop is kicked off to grow a new
        crawler version (inline when configured, else scheduled fire-and-forget).
        If T2 itself cannot extract, we still trigger the Loop and return empty
        items with a clear reason rather than crashing the request.

        The T2 re-fetch is wrapped so a fetch failure cannot escape ``request()``
        raw (the §8 contract: a known-family request returns a ``RequestResult``,
        only ``UnauthorizedDomain`` propagates):

        * ``UnauthorizedDomain`` (an off-list redirect mid re-fetch) is a hard
          policy stop — re-raised, never contained.
        * ``FetchBlocked`` means a block surfaced *now*, which is an ACCESS
          problem — routed to :meth:`_serve_blocked` (recovery), not a drift.
        * ``FetchError`` (or any other non-policy failure) means we cannot even
          fetch the page, so there is nothing to extract and no point growing a
          crawler against a page we can't read: return empty with a clear reason
          and DO NOT trigger the Loop (bounded — just report).
        """
        try:
            html = await self._ctx.fetch(url)
        except UnauthorizedDomain:
            raise
        except FetchBlocked:
            # A block now is an access problem, not a drift — escalate the access
            # ladder (recovery) instead of regenerating the crawler.
            return await self._serve_blocked(url, family)
        except FetchError as exc:
            # Can't fetch -> can't serve or heal: report it, skip the Loop.
            return RequestResult(
                items=[],
                source="fallback",
                family=family,
                reason=f"drift: refetch failed: {exc}",
            )
        try:
            items = await direct_extract(
                html, schema_ref, self._completer, model=self._model
            )
            reason = "drift->fallback"
        except ExtractionFailed as exc:
            items = []
            reason = f"drift->fallback failed: {exc.reason}"
        loop_result = await self._trigger_loop(family, schema_ref, seed_url=url)
        return RequestResult(
            items=items,
            source="fallback",
            family=family,
            loop=loop_result,
            reason=reason,
        )

    async def _serve_blocked(self, url: str, family: str) -> RequestResult:
        """BLOCKED_*: escalate the ACCESS ladder, then retry the family once.

        This is the access-recovery path, NOT the healing Loop: a block is an
        access problem (a 429/login wall/challenge), not a drifted extractor.
        :func:`recover_access` walks the domain's strategy ladder and persists a
        winner in the registry's access store. If it gets through we retry the
        ladder ONCE (the persisted strategy is now what the context fetches with);
        if that still fails, or recovery could not get through, we escalate to an
        empty result — we never loop forever on a block.
        """
        rec = await recover_access(
            url,
            config=self._config,
            access_store=self._registry,
            guarded=self._ctx.guarded,
            browser_runner=self._browser_runner,
            env=self._env,
            audit=self._audit_sink,
        )
        if rec.ok:
            try:
                run = await self._run_family(url, family)
            except UnauthorizedDomain:
                # A redirect off-list mid-retry is a hard policy stop, never
                # "recovered" — propagate it instead of swallowing it as a
                # blocked-escalation.
                raise
            except Exception:  # noqa: BLE001 — retry failed -> escalate, don't recurse
                return RequestResult(
                    items=[],
                    source="recovered",
                    family=family,
                    recovered_strategy=rec.strategy,
                    reason="blocked: recovered but retry failed",
                )
            return RequestResult(
                items=run.items,
                source="recovered",
                family=family,
                used_version=run.used_version,
                recovered_strategy=rec.strategy,
                reason="recovered",
            )
        return RequestResult(
            items=[],
            source="recovered",
            family=family,
            recovered_strategy=rec.strategy,
            reason="blocked: escalated",
        )

    async def _serve_transient(self, url: str, family: str) -> RequestResult:
        """TRANSIENT: retry the family up to ``max_transient_retries`` (capped).

        A transient failure (5xx / timeout / transport / anything unrecognized)
        is worth retrying as-is with a short exponential backoff between attempts
        (the sleep is injected so tests are instant). The retry count is bounded —
        on exhaustion we return empty with a "transient: gave up" reason rather
        than spinning. A retry that itself raises (any class) is caught and we
        keep retrying within the cap, so a block/drift surfacing only on retry
        still ends bounded rather than re-entering recovery/healing mid-backoff.
        """
        for attempt in range(self._max_transient_retries):
            await self._sleep(0.1 * 2**attempt)
            try:
                run = await self._run_family(url, family)
            except UnauthorizedDomain:
                # Off-list redirect mid-retry: hard policy stop, propagate.
                raise
            except Exception:  # noqa: BLE001 — keep retrying within the cap
                continue
            return RequestResult(
                items=run.items,
                source="registry",
                family=family,
                used_version=run.used_version,
                reason="ok",
            )
        return RequestResult(
            items=[], source="registry", family=family, reason="transient: gave up"
        )

    # -- bootstrap path ---------------------------------------------------- #

    async def _bootstrap(self, url: str, schema: str | None) -> RequestResult:
        """Unknown family: serve NOW via T2, register the family, trigger the Loop.

        A never-seen URL has no crawler, so the ONLY way to serve it is T2 — which
        needs the target ``schema`` (a model cannot extract to an unknown shape).
        With no schema this is a usage error -> :class:`EngineError`. Otherwise we
        extract with T2, register a family keyed on a derived URL pattern (so the
        next request to a sibling URL routes here) carrying the schema, and
        trigger the Loop to grow a first crawler version (inline or scheduled).
        """
        if schema is None:
            raise EngineError(f"unknown family for {url}: schema required")
        family = _derive_family(url)
        # The T2 fetch is wrapped so a fetch failure cannot escape ``request()``
        # raw. ``UnauthorizedDomain`` (a hard policy stop) propagates; a
        # ``FetchBlocked``/``FetchError`` means we could not even fetch the page,
        # so we DO NOT register a family (we'd be creating a family we can't fetch)
        # nor trigger the Loop — we just report the failure with a clear reason.
        try:
            html = await self._ctx.fetch(url)
        except UnauthorizedDomain:
            raise
        except (FetchBlocked, FetchError) as exc:
            return RequestResult(
                items=[],
                source="bootstrap",
                family=family,
                reason=f"bootstrap: fetch failed: {exc}",
            )
        try:
            items = await direct_extract(
                html, schema, self._completer, model=self._model
            )
            reason = "new family"
        except ExtractionFailed as exc:
            items = []
            reason = f"new family: fallback failed: {exc.reason}"
        # Register the family so future sibling URLs route here (a host+dir
        # derived pattern; the Loop will grow the crawler, not the routing).
        self._registry.upsert_family(
            family, [_derive_pattern(url)], schema, now=self._now
        )
        loop_result = await self._trigger_loop(family, schema, seed_url=url)
        return RequestResult(
            items=items,
            source="bootstrap",
            family=family,
            loop=loop_result,
            reason=reason,
        )

    # -- internals --------------------------------------------------------- #

    async def _run_family(self, url: str, family: str):
        """Run the registry ladder for ``family`` through the shared context.

        The module-level :func:`run_family` is referenced as ``engine.run_family``
        at call time (it is imported at module scope), so this single call site is
        what tests monkeypatch to inject transient/blocked failures.
        """
        return await run_family(
            family,
            url,
            self._ctx,
            registry=self._registry,
            validate=_validate,
            snapshots_dir=self._snapshots_dir,
            now=self._now,
        )

    async def _trigger_loop(
        self, family: str, schema_ref: str, *, seed_url: str
    ) -> LoopResult | None:
        """Kick off the regeneration Loop for ``family`` exactly once.

        Seeds with the family's recent-history URLs plus ``seed_url`` (deduped,
        order-preserving). When ``run_loop_inline`` is set the Loop is awaited and
        its :class:`LoopResult` returned; otherwise it is scheduled as a
        fire-and-forget background task (its result is not awaited) and ``None`` is
        returned — "serve now, heal in the background".

        In background mode the family is guarded against a concurrent second Loop
        (§8: "one in-flight job per family"): if a Loop for ``family`` is already
        in flight no second task is scheduled and ``None`` is returned. Inline
        mode is naturally serialized (the call is awaited before the next), so it
        is not guarded here.
        """
        seeds = self._loop_seeds(family, seed_url)
        if self._run_loop_inline:
            return await self._run_loop_once(family, schema_ref, seeds)
        # Background: one in-flight Loop per family. A duplicate trigger while one
        # is running is a no-op (return None) rather than a second full run.
        if family in self._inflight_families:
            return None
        self._inflight_families.add(family)
        task = asyncio.create_task(self._run_loop_guarded(family, schema_ref, seeds))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        # Clear the in-flight guard when the task finishes (success or failure) so
        # a later drift for the same family can heal again.
        task.add_done_callback(lambda _t: self._inflight_families.discard(family))
        # Retrieve any exception so a background failure never surfaces as an
        # "unhandled task exception" — the Loop is best-effort.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return None

    def _loop_seeds(self, family: str, seed_url: str) -> list[str]:
        """Recent-history URLs for ``family`` plus ``seed_url``, deduped in order.

        The history gives the Loop a few real pages of this family to sample; the
        current URL is always included (and de-duplicated) so even a brand-fresh
        family with no history still has at least one seed.
        """
        urls = [row["url"] for row in self._registry.recent_history(family)]
        urls.append(seed_url)
        seen: set[str] = set()
        deduped: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        return deduped

    async def _run_loop_once(
        self, family: str, schema_ref: str, seeds: list[str]
    ) -> LoopResult:
        """Run the Loop once, marking the family ``regenerating`` for the duration.

        Best-effort family-status bookkeeping so the status reflects reality
        (review M3): mark ``regenerating`` before the run, and after it mark
        ``healthy`` ONLY when the Loop ended cleanly (a promotion). An escalated
        Loop already set the family to ``escalated`` inside ``run_loop``, so we
        leave that alone. All status writes are wrapped (non-fatal): a brand-new
        family may have no row yet, and status is observability, not correctness.
        """
        self._set_family_status_safe(family, "regenerating")
        result = await run_loop(
            family,
            seeds,
            self._ctx,
            self._registry,
            self._completer,
            schema_ref,
            fixtures_dir=self._fixtures_dir,
            model=self._model,
            now=self._now,
        )
        if not result.escalated:
            self._set_family_status_safe(family, "healthy")
        return result

    def _set_family_status_safe(self, family: str, status: str) -> None:
        """Set a family's status, swallowing any error (best-effort, non-fatal).

        Status is observability only; a failure here (e.g. no family row yet for
        a brand-new family) must never break the serving path or the Loop.
        """
        try:
            self._registry.set_family_status(family, status)
        except Exception:  # noqa: BLE001 — status is best-effort, never fatal
            pass

    async def _run_loop_guarded(
        self, family: str, schema_ref: str, seeds: list[str]
    ) -> LoopResult | None:
        """Background Loop wrapper: swallow any failure (best-effort healing).

        A scheduled regeneration that cannot even start (e.g. the serving client
        was torn down) must not crash anything — the request already served. We
        return None on any error so the done-callback sees a clean result.
        """
        try:
            return await self._run_loop_once(family, schema_ref, seeds)
        except Exception:  # noqa: BLE001 — background healing is best-effort
            return None

    def _audit_sink(self, event: dict) -> None:
        """Mirror a recovery audit event into the registry's audit trail."""
        self._registry.write_audit(
            event.get("event", "access_recovered"), data=event, now=self._now
        )


# --------------------------------------------------------------------------- #
# Module-level helpers (pure / stateless)
# --------------------------------------------------------------------------- #


def _validate(items: list[dict], schema_ref: str):
    """The validate callable injected into :func:`run_family` (defaults only)."""
    return validator.validate(items, schema_ref)


def _derive_family(url: str) -> str:
    """A family key derived from ``url``: ``"<host>/<first-path-segment>"``.

    Kept deliberately simple (host + the leading path directory) so a bootstrap
    produces a stable, readable family that sibling listing URLs on the same host
    and section will route to. Falls back to just the host when there is no path
    segment.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return f"{host}/{segments[0]}"
    return host


def _derive_pattern(url: str) -> str:
    """A routing regex derived from ``url``'s host + leading path directory.

    Matches ``http(s)://<host>`` followed (anywhere) by the URL's first path
    segment, so sibling pages under the same section route to the bootstrapped
    family. Host and segment are ``re.escape``-d so dots/specials are literal.

    The segment is followed by ``(/|$)`` — an OPTIONAL trailing slash — so the
    pattern matches BOTH the path-less seed it was derived from (``…/p``) and a
    sibling under it (``…/p/2``). A required trailing slash would have failed to
    match its own seed, so the very next request to that seed would re-bootstrap
    instead of routing to the family just registered (M4).
    """
    parsed = urlparse(url)
    host = re.escape((parsed.hostname or "").lower())
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return rf"^https?://{host}.*/{re.escape(segments[0])}(/|$)"
    return rf"^https?://{host}"
