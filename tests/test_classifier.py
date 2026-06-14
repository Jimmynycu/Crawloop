"""Failure classifier (Task 8.1): exception / ValidationReport -> FailureClass.

One case per branch of :func:`classify`. No model or network is involved — every
case constructs a concrete exception or a :class:`ValidationReport` and asserts
the class. ``UnauthorizedDomain`` is special: it is a hard policy stop, not a
recoverable failure, so :func:`classify` re-raises it rather than returning a
class. A *successful* ``ValidationReport`` (``ok is True``) is a programming
error to classify and raises ``ValueError``.
"""

from __future__ import annotations

import asyncio

import pytest

from crawloop.access import FetchBlocked, FetchError
from crawloop.config import UnauthorizedDomain
from crawloop.executor import AllVersionsFailed
from crawloop.loop.trigger import FailureClass, classify, is_soft_404
from crawloop.validator import ValidationReport


# --- FetchBlocked: marker -> the three BLOCKED_* classes --------------------- #


def test_blocked_rate_marker():
    assert classify(FetchBlocked(status=429, marker="rate")) is FailureClass.BLOCKED_RATE


def test_blocked_auth_marker():
    assert classify(FetchBlocked(status=401, marker="auth")) is FailureClass.BLOCKED_AUTH


def test_blocked_challenge_marker():
    got = classify(FetchBlocked(status=403, marker="challenge"))
    assert got is FailureClass.BLOCKED_CHALLENGE


# --- FetchError: 404/410 -> GONE; everything else -> TRANSIENT --------------- #


def test_fetch_error_404_is_gone():
    assert classify(FetchError(status=404, cause=None)) is FailureClass.GONE


def test_fetch_error_410_is_gone():
    assert classify(FetchError(status=410, cause=None)) is FailureClass.GONE


def test_fetch_error_5xx_is_transient():
    assert classify(FetchError(status=503, cause=None)) is FailureClass.TRANSIENT


def test_fetch_error_no_status_transport_is_transient():
    # transport failure / timeout surfaced with no HTTP status
    err = FetchError(status=None, cause=TimeoutError("read timed out"))
    assert classify(err) is FailureClass.TRANSIENT


# --- AllVersionsFailed and failing ValidationReport -> DRIFT ----------------- #


def test_all_versions_failed_is_drift():
    exc = AllVersionsFailed(family="Product", reason="schema: 2/3 items invalid", last_report=None)
    assert classify(exc) is FailureClass.DRIFT


@pytest.mark.parametrize(
    "reason",
    ["empty", "schema: 1/3 items invalid", "fill_rate:price", "item_count: 1 < 0.5*10"],
)
def test_failing_validation_report_is_drift_for_every_reason(reason):
    report = ValidationReport(ok=False, reason=reason)
    assert classify(report) is FailureClass.DRIFT


def test_successful_validation_report_raises_valueerror():
    # Classifying a *success* is a programming error: nothing failed.
    report = ValidationReport(ok=True, reason="")
    with pytest.raises(ValueError):
        classify(report)


# --- bare timeout / connection exceptions -> TRANSIENT ----------------------- #


def test_timeout_error_is_transient():
    assert classify(TimeoutError()) is FailureClass.TRANSIENT


def test_asyncio_timeout_error_is_transient():
    assert classify(asyncio.TimeoutError()) is FailureClass.TRANSIENT


def test_connection_error_is_transient():
    assert classify(ConnectionError()) is FailureClass.TRANSIENT


# --- UnauthorizedDomain re-raises (hard policy stop, not a failure class) ----- #


def test_unauthorized_domain_is_reraised_not_classified():
    exc = UnauthorizedDomain("evil.example.com is not authorized")
    with pytest.raises(UnauthorizedDomain):
        classify(exc)


# --- unknown types default to TRANSIENT (conservative: retry, don't thrash) --- #


def test_unknown_exception_defaults_to_transient():
    assert classify(RuntimeError("something odd")) is FailureClass.TRANSIENT


def test_unknown_object_defaults_to_transient():
    assert classify(object()) is FailureClass.TRANSIENT


# --- soft-404 helper --------------------------------------------------------- #


def test_is_soft_404_detects_marker_case_insensitively():
    body = "<html><body><h1>Page Not Found</h1></body></html>"
    assert is_soft_404(body, ["not found"]) is True


def test_is_soft_404_false_when_no_marker():
    body = "<html><body><h1>3 results</h1></body></html>"
    assert is_soft_404(body, ["not found", "no longer exists"]) is False


def test_is_soft_404_false_for_empty_markers():
    assert is_soft_404("anything", []) is False
