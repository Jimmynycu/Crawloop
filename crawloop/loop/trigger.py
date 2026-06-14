"""The failure classifier: the trigger that routes a failure to a recovery mode.

When a fetch or an extraction fails, the engine (M10) needs to know *which kind*
of failure it is so it can pick the right response: retry, escalate the access
ladder, regenerate the crawler, or stop. :func:`classify` is that single
decision point. It takes the thing that went wrong — either an exception raised
by the access/executor layers or a failing :class:`~crawloop.validator.\
ValidationReport` — and maps it to one :class:`FailureClass`.

Design notes that the branches below depend on:

* :class:`~crawloop.config.UnauthorizedDomain` is **re-raised**, never
  classified. It is not a recoverable failure mode — it is a hard policy stop
  (the URL is off the allowlist). Routing it into healing would be a security
  regression (the engine must never "recover" its way onto an off-list host), so
  the classifier refuses to give it a class and lets it propagate.
* Unknown exception/object types default to :attr:`FailureClass.TRANSIENT`. The
  conservative move on something we did not anticipate is to *retry* it once or
  twice rather than to thrash crawler regeneration (the expensive path) on a
  failure we do not understand.
* A successful :class:`ValidationReport` (``ok is True``) is a programming error
  to classify — there is no failure to categorize — so it raises ``ValueError``.
"""

from __future__ import annotations

import asyncio
import enum

from crawloop.access import FetchBlocked, FetchError
from crawloop.config import UnauthorizedDomain
from crawloop.executor import AllVersionsFailed

# HTTP statuses that mean the resource is permanently gone, not transiently
# unavailable. A 5xx or a transport error is retryable; a 404/410 is not — the
# page is dead and the right response is to stop, not to heal.
_GONE_STATUSES = frozenset({404, 410})


class FailureClass(enum.Enum):
    """The mutually-exclusive kinds of failure the loop knows how to route.

    * ``TRANSIENT`` — a hiccup worth retrying as-is (5xx, timeout, transport
      error, or anything unrecognized): same crawler, same access strategy.
    * ``DRIFT`` — the extractor is wrong for the current page (every version on
      the ladder failed, or validation failed): the site changed; regenerate.
    * ``BLOCKED_RATE`` — throttled (HTTP 429): back off / slow down.
    * ``BLOCKED_AUTH`` — an authentication wall (401/403 login): escalate to a
      credentialed access strategy.
    * ``BLOCKED_CHALLENGE`` — an anti-bot challenge page (Cloudflare-style):
      escalate to a browser/stealth access strategy.
    * ``GONE`` — the resource is permanently gone (404/410): stop, do not heal.
    """

    TRANSIENT = "transient"
    DRIFT = "drift"
    BLOCKED_RATE = "blocked_rate"
    BLOCKED_AUTH = "blocked_auth"
    BLOCKED_CHALLENGE = "blocked_challenge"
    GONE = "gone"


# A blocked-marker maps 1:1 to a BLOCKED_* class. Defined once so the mapping is
# data, not a chain of ``if`` arms, and an unexpected marker falls through to the
# conservative TRANSIENT default rather than raising.
_BLOCKED_MARKER_TO_CLASS = {
    "rate": FailureClass.BLOCKED_RATE,
    "auth": FailureClass.BLOCKED_AUTH,
    "challenge": FailureClass.BLOCKED_CHALLENGE,
}


def classify(obj: object) -> FailureClass:
    """Classify a failure (an exception OR a failing ``ValidationReport``).

    Returns the :class:`FailureClass` the engine should route on. Two non-return
    paths, both deliberate:

    * a :class:`~crawloop.config.UnauthorizedDomain` is **re-raised** (it is a
      hard policy stop, not a recoverable failure — see the module docstring);
    * a *successful* ``ValidationReport`` (``ok is True``) raises ``ValueError``
      (there is no failure to classify).

    Recognized inputs and their classes:

    * ``FetchBlocked`` -> ``BLOCKED_RATE`` / ``BLOCKED_AUTH`` /
      ``BLOCKED_CHALLENGE`` by its ``marker``.
    * ``FetchError`` -> ``GONE`` for status 404/410, else ``TRANSIENT`` (5xx,
      timeout, transport — no status or a retryable one).
    * ``AllVersionsFailed`` -> ``DRIFT``.
    * a ``ValidationReport`` with ``ok is False`` -> ``DRIFT`` (every failing
      ``reason`` — empty / schema / fill_rate* / item_count — means the extractor
      is producing the wrong output, i.e. the site drifted).
    * ``TimeoutError`` / ``asyncio.TimeoutError`` / ``ConnectionError`` ->
      ``TRANSIENT``.
    * anything else -> ``TRANSIENT`` (conservative default: retry, don't thrash
      regeneration on an unrecognized failure).
    """
    # Hard policy stop FIRST: never give an off-allowlist failure a recovery
    # class. Re-raise so the engine cannot route it into healing.
    if isinstance(obj, UnauthorizedDomain):
        raise obj

    if isinstance(obj, FetchBlocked):
        return _BLOCKED_MARKER_TO_CLASS.get(obj.marker or "", FailureClass.TRANSIENT)

    if isinstance(obj, FetchError):
        if obj.status in _GONE_STATUSES:
            return FailureClass.GONE
        # 5xx, or no status (timeout/transport) -> retryable.
        return FailureClass.TRANSIENT

    if isinstance(obj, AllVersionsFailed):
        return FailureClass.DRIFT

    # A ValidationReport is identified structurally (it is not an exception and
    # exposes ``ok``), so we never import its concrete type at runtime. A pass is
    # not a failure to classify; any fail reason means the extractor drifted.
    if _is_validation_report(obj):
        if obj.ok:  # type: ignore[attr-defined]
            raise ValueError(
                "classify() was given a successful ValidationReport (ok=True); "
                "there is no failure to classify"
            )
        return FailureClass.DRIFT

    if isinstance(obj, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return FailureClass.TRANSIENT

    # Unrecognized: retry rather than regenerate (see module docstring).
    return FailureClass.TRANSIENT


def _is_validation_report(obj: object) -> bool:
    """Whether ``obj`` looks like a :class:`ValidationReport`.

    Recognized by shape (an ``ok: bool`` and a ``reason``) rather than by
    ``isinstance`` so this module does not import the validator at runtime, and
    so it never mistakes an exception (which has neither) for a report.
    """
    if isinstance(obj, BaseException):
        return False
    return isinstance(getattr(obj, "ok", None), bool) and hasattr(obj, "reason")


def is_soft_404(body: str, markers: list[str]) -> bool:
    """Whether a 200-OK ``body`` is really a "not found" page in disguise.

    Some sites return HTTP 200 with a "page not found"/"no longer available"
    body instead of a real 404. This is a small, case-insensitive substring
    check over ``body`` for any of ``markers``; the engine decides *when* to call
    it (it has the status and the body), because :func:`classify` itself works
    only on the exception/report it is handed, not on raw HTML.
    """
    if not markers:
        return False
    lowered = body.lower()
    return any(marker.lower() in lowered for marker in markers)
