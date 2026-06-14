"""The access-recovery loop (Task 4.4).

When an inline fetch is blocked, :class:`~crawloop.access.RealFetchContext`
raises rather than escalating. *This* is where escalation happens: build the
domain's ordered strategy ladder (the saved working strategy first, then every
configured rung, de-duplicated by name) and walk it for up to ``max_rounds``
rounds. The first strategy that comes back ``ok`` is persisted as the domain's
working strategy and returned; if the whole ladder fails every round, the domain
is marked ``"escalated"`` (a human/operator decision is now needed).

Strategies are built through the one shared
:func:`~crawloop.access.build_strategy` factory, so the config-kind ->
strategy mapping lives in exactly one place (the same one the fetch context
uses). A :class:`~crawloop.access.NotEnabled` from an unauthorized
:class:`~crawloop.access.CaptchaSolver` rung means "this strategy is
unavailable" and is skipped, not fatal.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from crawloop.access import (
    AccessStore,
    AccessStrategy,
    BrowserRunner,
    GuardedClient,
    NotEnabled,
    build_strategy,
)
from crawloop.config import AppConfig

# Optional sink for a structured audit event on success. Kept as a bare callable
# so callers (M5/M10) can pass any logger/recorder without this module importing
# their concrete types.
AuditSink = Callable[[dict], None]


@dataclass
class RecoveryResult:
    """Outcome of a recovery attempt.

    * ``ok`` — did some strategy get through?
    * ``strategy`` — the winning strategy's name (``None`` on failure).
    * ``rounds`` — the round the winner succeeded in (1-based), or ``max_rounds``
      when every round was exhausted without success.
    """

    ok: bool
    strategy: str | None
    rounds: int


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _build_ladder(
    url: str,
    *,
    config: AppConfig,
    access_store: AccessStore,
    guarded: GuardedClient,
    browser_runner: BrowserRunner,
    env: Mapping[str, str],
) -> list[AccessStrategy]:
    """The ordered, de-duplicated strategy ladder for ``url``'s domain.

    Order: the saved working strategy (if any) first, then every configured
    ``(kind, params)`` for the domain. De-duplication is by the *built*
    strategy's ``name`` so a saved winner that is also a configured rung (or a
    rung listed twice) is only tried once per round. The saved winner's params
    are looked up from the configured rungs so it rebuilds with its real headers
    (every strategy's ``name`` equals its config kind, so the lookup is by kind).

    Every GET-based rung is built with the shared :class:`GuardedClient`, so each
    recovery attempt is allowlist-checked and rate-limited (I1) just like the
    inline fetch path.
    """
    host = _host(url)
    dc = config.domain_config(host)
    configured = list(dc.access_strategies)

    ordered: list[tuple[str, dict]] = []
    saved = access_store.get_working_strategy(host)
    if saved is not None:
        saved_params = next((p for k, p in configured if k == saved), {})
        ordered.append((saved, saved_params))
    ordered.extend(configured)

    ladder: list[AccessStrategy] = []
    seen: set[str] = set()
    for kind, params in ordered:
        strategy = build_strategy(
            kind, params, guarded=guarded, browser_runner=browser_runner, env=env
        )
        if strategy.name in seen:
            continue
        seen.add(strategy.name)
        ladder.append(strategy)
    return ladder


async def recover_access(
    url: str,
    *,
    config: AppConfig,
    access_store: AccessStore,
    guarded: GuardedClient,
    browser_runner: BrowserRunner,
    env: Mapping[str, str] = os.environ,
    max_rounds: int = 2,
    audit: AuditSink | None = None,
) -> RecoveryResult:
    """Walk the domain's strategy ladder until one gets through, or escalate.

    Off-list URLs raise :class:`~crawloop.config.UnauthorizedDomain` (the
    allowlist gate is never bypassed — and because every GET-based rung goes
    through the shared :class:`GuardedClient`, a cross-host/SSRF redirect mid
    recovery is blocked too, and every attempt is rate-limited). On the first
    ``ok`` outcome the winning strategy name is persisted via
    ``access_store.set_working_strategy`` and an optional ``audit`` event is
    emitted. If every strategy fails in every round,
    ``access_store.mark_domain_status(host, "escalated")`` is called.
    """
    config.assert_authorized(url)  # off-list -> UnauthorizedDomain (uncaught)
    host = _host(url)
    ladder = _build_ladder(
        url,
        config=config,
        access_store=access_store,
        guarded=guarded,
        browser_runner=browser_runner,
        env=env,
    )

    for round_no in range(1, max_rounds + 1):
        for strategy in ladder:
            try:
                outcome = await strategy.fetch(url)
            except NotEnabled:
                # Strategy is unavailable for this domain (e.g. captcha solving
                # not authorized). Treat as "rung absent" and keep walking.
                continue
            if outcome.is_ok:
                access_store.set_working_strategy(host, strategy.name)
                if audit is not None:
                    audit(
                        {
                            "event": "access_recovered",
                            "host": host,
                            "strategy": strategy.name,
                            "round": round_no,
                        }
                    )
                return RecoveryResult(ok=True, strategy=strategy.name, rounds=round_no)

    access_store.mark_domain_status(host, "escalated")
    return RecoveryResult(ok=False, strategy=None, rounds=max_rounds)
