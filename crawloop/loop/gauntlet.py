"""The Loop's GAUNTLET step (Task 9.4): score candidate crawlers, pick a winner.

Codegen (Task 9.2) hands the loop gate-passing candidate *source*; the gauntlet
is the JUDGE that decides whether any of them is good enough to promote. For each
candidate it sandbox-runs the source (Task 9.3) against the sample pages and then
scores the output purely with the M7 validator gates — this module never fetches
and never decides the bar by itself, it composes :func:`run_in_sandbox` with
:func:`validate` / :func:`oracle_agreement` / :func:`fixture_regression`.

A candidate is scored on four axes (design §9):

* **schema_valid** — every sample's extraction must pass
  :func:`~crawloop.validator.validate` (a sample that crashes in the sandbox
  is *not* schema-valid);
* **oracle_agreement** — the reported mean, over samples, of agreement with that
  sample's oracle JSON (Gate 3); used for ranking and the failure report;
* **fixture_pass** — the reported mean, over stored fixtures, of fixture
  regression (Gate 4); a family with NO fixtures passes this vacuously (1.0),
  because a brand-new family has nothing to regress against. A fixture is
  ``(html, expected_items)`` with no per-fixture URL, so it is replayed at the
  sandbox's canonical default URL; the ``url`` field of ``expected_items`` is
  therefore compared as absolutized against that base (the content fields name/
  price/in_stock are URL-independent);
* **min_item_agreement** / **counts_match** — the GATE signals: the lowest
  per-item agreement across every sample AND fixture, and whether the candidate's
  item count matched the oracle/fixture on every one. These are what the bar
  actually tests — a MEAN dilutes a single wrong or dropped row away
  (``mean([1.0]*99 + [0.0]) = 0.99``), so the gate is on the per-item minimum and
  an exact item-count match instead;
* **exec_errors** — how many samples/fixtures raised :class:`SandboxError` /
  :class:`SandboxTimeout` (a crashing candidate is never promotable).

The acceptance bar (the §9 "promote only when it clears every gate" rule):

    ``passed = schema_valid AND counts_match
               AND min_item_agreement >= 0.98
               AND fixture_pass >= 1.0 AND exec_errors == 0``

i.e. EVERY item of EVERY sample and fixture must agree with its counterpart at or
above the bar, with matching item counts — no averaging can let a wrong crawler
through.

:func:`run_gauntlet` scores every candidate and returns ``(best, all_scores)``:
``best`` is the PASSING candidate with the highest (reported mean) oracle
agreement (or ``None`` if none passed), and ``all_scores`` is every candidate's
score — the loser scores are what the driver turns into the next round's failure
report.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawloop.loop.sandbox import SandboxError, SandboxTimeout, run_in_sandbox
from crawloop.validator import AgreementDetail, agreement_detail, validate

# The design §9 promotion bar. EVERY item of EVERY sample must agree with its
# oracle counterpart at least this much — the gate is on the per-item MINIMUM
# (and a matching item count), not a mean, because a mean dilutes a single wrong
# row away: mean([1.0]*99 + [0.0]) = 0.99 would clear a mean bar while hiding a
# fully-wrong row. Kept as a named constant so the bar lives in exactly one place.
_AGREEMENT_BAR = 0.98

# Fixtures are golden: a promoted crawler must reproduce every stored fixture
# exactly (mean regression == 1.0). No tolerance here — a fixture regression is
# a known-good page the new code must not break.
_FIXTURE_BAR = 1.0


@dataclass
class CandidateScore:
    """One candidate's gauntlet result.

    ``source`` is the candidate verbatim (so the driver can promote the winner
    without re-deriving it). ``schema_valid`` is True only when EVERY sample's
    sandbox output passed the schema gate. ``oracle_agreement`` / ``fixture_pass``
    are the reported MEANS (over samples / fixtures) described in the module
    docstring — kept for ranking and the failure report. ``min_item_agreement``
    is the LOWEST per-item agreement across every sample AND fixture (the value
    the promotion gate actually tests, so one wrong row cannot be averaged away);
    ``counts_match`` is True only when the candidate's item count matched the
    oracle/fixture on EVERY sample and fixture. ``exec_errors`` counts
    samples/fixtures that crashed/timed out in the sandbox. ``passed`` is the §9
    acceptance bar. ``detail`` is a short human/loggable summary.
    """

    source: str
    schema_valid: bool
    oracle_agreement: float
    fixture_pass: float
    min_item_agreement: float
    counts_match: bool
    exec_errors: int
    passed: bool
    detail: str


def _mean(values: list[float]) -> float:
    """Arithmetic mean, or 1.0 for an empty list.

    Empty means "nothing to judge here": no samples to agree with, or no
    fixtures to regress against. Both are vacuous passes (a family with no
    fixtures must not be blocked on fixture regression), so the neutral value is
    1.0 rather than 0.0.
    """
    return sum(values) / len(values) if values else 1.0


def score_candidate(
    source: str,
    samples: list[tuple[str, str]],
    oracle_jsons: list[list[dict]],
    fixtures: list[tuple[str, list[dict]]],
    schema_ref: str,
    *,
    agreement_bar: float = _AGREEMENT_BAR,
) -> CandidateScore:
    """Sandbox-run ``source`` against the samples + fixtures and score it.

    ``samples`` is ``[(url, html), ...]``; ``oracle_jsons`` is the oracle's
    trusted records for each sample, aligned by position; ``fixtures`` is
    ``[(html, expected_items), ...]`` golden pages. For each sample the candidate
    is run in the sandbox at that sample's URL: on a sandbox crash/timeout the
    sample counts as an exec error, its schema validity is False, and its
    agreement detail is the empty/zero detail; otherwise the sample's extraction
    is schema-checked (:func:`validate`) and compared to its oracle per item
    (:func:`agreement_detail`). ``schema_valid`` is True only if every sample
    passed the schema gate.
    ``oracle_agreement`` is the reported mean over samples. Each fixture is
    likewise run and scored; ``fixture_pass`` is the reported mean over fixtures
    (1.0 when there are none).

    The GATE, however, is per item, per sample/fixture: for every sample AND every
    fixture we take :func:`agreement_detail`, and that source "agrees" only if its
    item count matched (``count_match``) AND its worst item cleared the bar
    (``min_item >= _AGREEMENT_BAR``). ``min_item_agreement`` is the minimum
    ``min_item`` over all samples and fixtures; ``counts_match`` is True only if
    every sample and fixture matched counts. A sandbox crash/timeout is an exec
    error and contributes a 0.0/non-matching detail. ``passed`` applies the §9
    bar to those aggregates.
    """
    exec_errors = 0
    schema_flags: list[bool] = []
    agreements: list[float] = []
    # Per-item gate signals, pooled over samples AND fixtures: every entry must
    # clear the bar and match counts for the candidate to pass.
    min_items: list[float] = []
    count_matches: list[bool] = []

    def _record(detail: AgreementDetail) -> None:
        agreements.append(detail.mean)
        min_items.append(detail.min_item)
        count_matches.append(detail.count_match)

    def _record_exec_error() -> None:
        # A run the candidate could not complete is a hard fail on that page: no
        # agreement, worst item 0.0, and counts cannot be said to match.
        nonlocal exec_errors
        exec_errors += 1
        agreements.append(0.0)
        min_items.append(0.0)
        count_matches.append(False)

    for (url, html), oracle in zip(samples, oracle_jsons):
        try:
            actual = run_in_sandbox(source, html, url=url)
        except (SandboxError, SandboxTimeout):
            # A candidate that can't even run on this page is neither
            # schema-valid nor in agreement here; record the exec error.
            schema_flags.append(False)
            _record_exec_error()
            continue
        schema_flags.append(validate(actual, schema_ref).ok)
        _record(agreement_detail(actual, oracle, schema_ref))

    fixture_scores: list[float] = []
    for fx_html, expected in fixtures:
        try:
            actual = run_in_sandbox(source, fx_html)
        except (SandboxError, SandboxTimeout):
            # A fixture the candidate can't run is a hard regression on that
            # known-good page.
            fixture_scores.append(0.0)
            _record_exec_error()
            continue
        detail = agreement_detail(actual, expected, schema_ref)
        fixture_scores.append(detail.mean)
        _record(detail)

    schema_valid = bool(schema_flags) and all(schema_flags)
    agreement = _mean(agreements)
    fixture_pass = _mean(fixture_scores)
    # The gate aggregates: the worst item over everything, and whether EVERY
    # sample/fixture matched item counts. Empty (no samples and no fixtures) ->
    # a vacuous perfect minimum and matching counts, same neutral as the means.
    min_item_agreement = min(min_items) if min_items else 1.0
    counts_match = all(count_matches)
    passed = (
        schema_valid
        and counts_match
        and min_item_agreement >= agreement_bar
        and fixture_pass >= _FIXTURE_BAR
        and exec_errors == 0
    )
    detail = (
        f"schema_valid={schema_valid} oracle_agreement={agreement:.3f} "
        f"fixture_pass={fixture_pass:.3f} min_item_agreement={min_item_agreement:.3f} "
        f"counts_match={counts_match} exec_errors={exec_errors} -> "
        f"{'PASS' if passed else 'FAIL'}"
    )
    return CandidateScore(
        source=source,
        schema_valid=schema_valid,
        oracle_agreement=agreement,
        fixture_pass=fixture_pass,
        min_item_agreement=min_item_agreement,
        counts_match=counts_match,
        exec_errors=exec_errors,
        passed=passed,
        detail=detail,
    )


def run_gauntlet(
    candidates: list[str],
    samples: list[tuple[str, str]],
    oracle_jsons: list[list[dict]],
    fixtures: list[tuple[str, list[dict]]],
    schema_ref: str,
    *,
    agreement_bar: float = _AGREEMENT_BAR,
) -> tuple[CandidateScore | None, list[CandidateScore]]:
    """Score every candidate and return ``(best_passing, all_scores)``.

    Each candidate is scored with :func:`score_candidate`. ``best_passing`` is
    the candidate among those that ``passed`` with the highest
    ``oracle_agreement`` (ties broken by first occurrence, so the result is
    deterministic), or ``None`` if none passed. ``all_scores`` is every
    candidate's score in the given order — the failing ones are what the driver
    summarises into the next round's failure report.
    """
    all_scores = [
        score_candidate(
            source, samples, oracle_jsons, fixtures, schema_ref, agreement_bar=agreement_bar
        )
        for source in candidates
    ]
    passing = [s for s in all_scores if s.passed]
    # max() keeps the FIRST element on ties (it does not replace on equal keys),
    # so the winner is deterministic given the candidate order.
    best = max(passing, key=lambda s: s.oracle_agreement) if passing else None
    return best, all_scores
