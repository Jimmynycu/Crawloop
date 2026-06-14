"""The full extraction Loop driver (Task 9.5): the regeneration state machine.

This is the conductor M9 builds toward: one coroutine, :func:`run_loop`, that
drives a family from "we need a crawler / the current one broke" to either a
promoted new version or an escalation, composing the steps each earlier task
built and adding NOTHING of its own logic beyond the wiring and the round budget.

The pipeline (design §9):

1. **SAMPLE** — :func:`~crawloop.loop.sampler.collect_samples` fetches a few
   fresh pages of the family. No pages -> escalate ("no samples").
2. **ORACLE** — per sample, :func:`~crawloop.fallback.direct_extract` (T2)
   produces the trusted "what the answer should be". A sample whose oracle fails
   (:class:`ExtractionFailed`) is DROPPED (page + its slot), because generating
   against a bad ground truth is worse than not generating. Fewer than
   ``min_oracles`` (default 3) usable oracles left -> escalate ("insufficient
   oracles: got N, need M"): >= 3 independent samples are the design's bound on
   the LLM oracle's own error (§2/§9/§15), so promoting against 1-2 is the
   wrong-crawler risk. We never generate against garbage, nor against too little.
3. **CONTEXT** — load the family's golden fixtures
   (:func:`~crawloop.loop.promote.load_fixtures`) for the gauntlet's
   regression gate, and the active version's source
   (:meth:`Registry.active_source`) as ``prev_source`` for codegen. A brand-new
   family simply has no fixtures and ``prev_source=None`` — the SAME code path,
   so new-family bootstrap is just ``run_loop`` with nothing registered yet. Two
   DETERMINISTIC candidates are also built here for the FIRST round's gauntlet when
   the samples embed a JSON island: a VALUE-PATH crawler
   (:func:`_value_path_candidate`, no LLM — reads fields whose oracle value appears
   verbatim in the JSON) and a PATH-MAP crawler (:func:`_path_map_candidate`, one LLM
   call to propose a ``{field: {path, transform}}`` map — reaches NORMALIZED fields:
   scaled prices, enum codes, concatenated strings). Each is a candidate like any
   other; either is promoted only if it clears every gate, and neither blocks the LLM
   codegen rounds when it does not apply.
4. **ROUNDS** (1..``max_rounds``) — :func:`~crawloop.loop.codegen.generate_candidates`
   writes ``k`` candidate sources; :func:`~crawloop.loop.gauntlet.run_gauntlet`
   scores them. A passing winner is PROMOTED
   (:func:`~crawloop.loop.promote.promote`) and we return ``ok``. Otherwise
   the round's scores become a concise ``failure_report`` fed into the next
   round's prompt, and we try again.
5. **EXHAUSTED** — ``max_rounds`` with no winner -> escalate ("max rounds
   exhausted").

Every escalation goes through one helper (:func:`_escalate`) so the audit entry
+ family-status update + :class:`LoopResult` are produced in exactly one place.
"""

from __future__ import annotations

import enum
import typing
from dataclasses import dataclass
from pathlib import Path

from crawloop.contract import FetchContext
from crawloop.fallback import ExtractionFailed, direct_extract
from crawloop.hybrid import compute_residual_fields
from crawloop.llm import Completer, escalation_model
from crawloop.loop.codegen import generate_candidates
from crawloop.loop.gauntlet import CandidateScore, run_gauntlet
from crawloop.loop.jsonpath import (
    JSON_MARKERS,
    discover_paths,
    emit_crawler,
    extract_json_blobs,
)
from crawloop.loop.pathmap import (
    emit_crawler as emit_pathmap_crawler,
)
from crawloop.loop.pathmap import (
    propose_fieldmap,
)
from crawloop.loop.promote import load_fixtures, promote
from crawloop.loop.sampler import collect_samples
from crawloop.loop.sandbox import SandboxError, SandboxTimeout, run_in_sandbox
from crawloop.registry import Registry
from crawloop.schemas import get_schema
from crawloop.validator import history_crosscheck


@dataclass
class LoopResult:
    """The outcome of one :func:`run_loop`.

    ``ok`` is True only when a new version was promoted (``version`` then carries
    its number and ``reason == "promoted"``). On any escalation ``ok`` is False,
    ``escalated`` is True, ``version`` is None, and ``reason`` says why ("no
    samples" / "insufficient oracles: got N, need M" / "max rounds exhausted").
    ``rounds`` is how many codegen+gauntlet rounds ran (0 if we escalated before
    the round loop).
    """

    ok: bool
    version: int | None
    rounds: int
    escalated: bool
    reason: str


def _score_summary(score: CandidateScore) -> dict:
    """A JSON-serialisable summary of one candidate's gauntlet score.

    Used both for the promote audit (the winner) and inside the failure report
    (the losers). Floats and ints only, so it survives ``json.dumps`` in the
    audit row without a custom encoder.
    """
    return {
        "schema_valid": score.schema_valid,
        "oracle_agreement": score.oracle_agreement,
        "fixture_pass": score.fixture_pass,
        "min_item_agreement": score.min_item_agreement,
        "counts_match": score.counts_match,
        "exec_errors": score.exec_errors,
        "passed": score.passed,
    }


def _history_warnings(
    registry: Registry,
    family: str,
    winner_source: str,
    sample: tuple[str, str],
    schema_ref: str,
) -> list[str]:
    """Gate 5 at promote time: cross-check the winner's extraction vs history.

    Runs the winning candidate ``winner_source`` against ONE sample page (in the
    same sandbox the gauntlet used) and compares its items to the family's most
    recent prior extraction via :func:`~crawloop.validator.history_crosscheck`
    — the design's "VAT-shift / swapped-field" alarm. The result is ADVISORY
    only: it is recorded in the promote audit, never blocks promotion (Gate 5 is
    non-fatal at runtime). Wholly best-effort: a sandbox crash/timeout or any
    other failure yields ``[]`` rather than derailing a promotion. With no prior
    history :func:`history_crosscheck` returns ``[]`` (no behavior change).
    """
    url, html = sample
    try:
        items = run_in_sandbox(winner_source, html, url=url)
    except (SandboxError, SandboxTimeout):
        # The winner already cleared the gauntlet on this page; a re-run crash
        # here is not a reason to skip promotion — just emit no warnings.
        return []
    history_rows = registry.recent_history(family, url=url)
    return history_crosscheck(items, history_rows, schema_ref)


def _residual_fields(
    winner_source: str,
    usable_samples: list[tuple[str, str]],
    usable_oracles: list[list[dict]],
    schema_ref: str,
) -> list[str]:
    """The hybrid RESIDUAL SET for the promoted ``winner_source`` (computed at promote).

    Re-runs the winning candidate against EACH usable sample in the same sandbox the
    gauntlet used (so a deterministic crawler that systematically omits a field shows
    that gap on its real output), then derives the residual set vs the usable oracles
    via :func:`crawloop.hybrid.compute_residual_fields`: the fields the oracle
    populates that the crawler never reaches. This is the set the RUNTIME hybrid will
    tail-fill with one small LLM call; ``[]`` (the common case for a complete crawler)
    means ZERO LLM calls at runtime.

    Best-effort, exactly like :func:`_history_warnings`: a sample that crashes/times
    out in the sandbox contributes an EMPTY output (counted as missing every field) so
    it cannot mask a real gap, and a re-run failure never derails the promotion. The
    winner already cleared the gauntlet, so its outputs here are reliable enough to
    diff against the oracle.
    """
    crawler_outputs: list[list[dict]] = []
    for url, html in usable_samples:
        try:
            crawler_outputs.append(run_in_sandbox(winner_source, html, url=url))
        except (SandboxError, SandboxTimeout):
            crawler_outputs.append([])  # crashed here -> produced nothing on this page
    return compute_residual_fields(crawler_outputs, usable_oracles, schema_ref)


def _failure_report(round_no: int, scores: list[CandidateScore]) -> str:
    """A concise, model-facing summary of why a round produced no winner.

    Lists each candidate's deciding signals (schema validity, oracle agreement,
    fixture pass, exec errors) so the next round's codegen prompt can see HOW the
    last attempts fell short (wrong element vs crash vs schema) and aim the fix.
    Empty candidate list reads as "the model emitted no safe candidate".
    """
    if not scores:
        return f"Round {round_no}: no gate-passing candidate was produced."
    lines = [f"Round {round_no}: {len(scores)} candidate(s), none passed the gauntlet."]
    for i, s in enumerate(scores, start=1):
        lines.append(f"  candidate {i}: {s.detail}")
    return "\n".join(lines)


def _escalate(
    registry: Registry,
    family: str,
    schema_ref: str,
    *,
    reason: str,
    rounds: int,
    now: str | None,
    data: dict,
) -> LoopResult:
    """Record an escalation and return the escalated :class:`LoopResult`.

    The single place escalation side effects happen: write an ``"escalated"``
    audit entry, then mark the family ``escalated`` so the state is observable
    via :meth:`Registry.get_family`. A family that escalated before any version
    was registered (no samples, or the oracle failed) has no families row yet —
    :meth:`Registry.set_family_status` would raise on it — so we create a minimal
    row first (preserving an existing one: ``upsert_family`` only touches the row
    when it is absent here, since we gate on ``get_family``). The audit entry is
    the durable record; the status is the at-a-glance flag.
    """
    registry.write_audit(
        "escalated", family=family, data={"reason": reason, **data}, now=now
    )
    if registry.get_family(family) is None:
        # Brand-new family that never got a version: create a minimal row so the
        # escalated status has somewhere to live.
        registry.upsert_family(family, [], schema_ref, now=now)
    registry.set_family_status(family, "escalated")
    return LoopResult(
        ok=False, version=None, rounds=rounds, escalated=True, reason=reason
    )


def _enum_fields(schema_ref: str) -> set[str]:
    """Names of ``schema_ref``'s ENUM-typed fields (incl. ``Enum | None``).

    These are the fields the deterministic value-path discovery cannot find: their
    oracle value is a normalized code (``"full_time"``/``"unknown"``) that does not
    appear verbatim in the page JSON (which stores the source label, e.g.
    ``"Full-time"``). Value->path matching only finds verbatim values, so enum fields
    are passed to
    :func:`~crawloop.loop.jsonpath.discover_paths` as ``skip_fields`` and left to
    LLM codegen, which can map the source label to the enum member. A field is enum
    iff its annotation IS an ``Enum`` subclass or has one among its union args.
    """
    model = get_schema(schema_ref)
    out: set[str] = set()
    for name, info in model.model_fields.items():
        annotation = info.annotation
        candidates = [annotation, *typing.get_args(annotation)]
        if any(isinstance(c, type) and issubclass(c, enum.Enum) for c in candidates):
            out.add(name)
    return out


def _value_path_candidate(
    family: str,
    usable_samples: list[tuple[str, str]],
    usable_oracles: list[list[dict]],
    schema_ref: str,
) -> str | None:
    """Build a deterministic value-path crawler source for ``family``, or None.

    The "no-LLM-guessing" candidate (design: deterministic JSON-path strategy). For
    server-rendered pages the complete record sits in a ``__NEXT_DATA__`` /
    ``application/(ld+)?json`` island; rather than ask the model to navigate that
    deep JSON, we use the oracle to DISCOVER where each value lives and emit a tiny
    crawler that reads those exact paths.

    Returns ``None`` (so the loop just relies on LLM codegen) when the strategy does
    not apply: any sample has NO JSON island, or no field had a cross-sample-
    consistent path. Otherwise returns a complete, AST-gate-clean crawler module
    source (the caller hands it to the gauntlet like any other candidate; it is
    promoted only if it clears every gate).

    Discovery inputs: each sample's FIRST parseable JSON island (the same island the
    emitted crawler reads at run time) and that sample's first oracle record (a
    detail page is one record). ``numeric_fields`` is the schema's VOLATILE set (so
    unit scaling is handled); ``skip_fields`` is the schema's enum fields (left to
    LLM codegen — see :func:`_enum_fields`).
    """
    sample_jsons: list = []
    for _url, html in usable_samples:
        blobs = extract_json_blobs(html)
        if not blobs:
            return None  # no JSON island on this sample -> strategy does not apply
        sample_jsons.append(blobs[0])

    # A detail page is one record; use each sample's first oracle record. An empty
    # oracle list yields {} (no fields), which simply contributes nothing to match.
    oracle_records = [records[0] if records else {} for records in usable_oracles]

    model = get_schema(schema_ref)
    discovery = discover_paths(
        sample_jsons,
        oracle_records,
        numeric_fields=set(getattr(model, "VOLATILE", set())),
        skip_fields=_enum_fields(schema_ref),
    )
    if not discovery.paths:
        return None  # nothing discoverable -> let LLM codegen handle the family

    return emit_crawler(
        family=family,
        schema_ref=schema_ref,
        discovery=discovery,
        json_markers=JSON_MARKERS,
    )


async def _path_map_candidate(
    family: str,
    usable_samples: list[tuple[str, str]],
    usable_oracles: list[list[dict]],
    schema_ref: str,
    completer: Completer,
    model: str,
) -> str | None:
    """Build a deterministic PATH-MAP crawler source for ``family``, or None.

    The complement to :func:`_value_path_candidate`: that one finds fields whose
    oracle value appears VERBATIM in the page JSON, but it cannot find NORMALIZED
    fields — a unit-scaled number, an enum code the JSON stores as a source label, a
    concatenated ``location``. For those we ask the model ONCE (the only LLM call
    this strategy makes) to propose a declarative ``{field: {path, transform}}`` map
    over the first usable sample, then emit a free, deterministic crawler that
    executes it (:func:`~crawloop.loop.pathmap.emit_crawler`).

    Returns ``None`` — so the loop just relies on the other candidates — whenever the
    strategy does not apply or anything fails: no usable sample, the sample has no
    JSON island, the model's map cannot be parsed, or it reproduces too little of the
    oracle (:class:`~crawloop.loop.pathmap.FieldmapProposalError`). Any such
    failure is swallowed here (never fatal); a returned source is a candidate like any
    other and is promoted only if it clears every gate.

    The proposal uses the FIRST usable sample's HTML + its first oracle record (a
    listing detail page is one record); the emitted crawler reads the FIRST JSON
    island at run time, exactly the island the proposal mapped against.
    """
    if not usable_samples:
        return None
    _url, sample_html = usable_samples[0]
    oracle_records = usable_oracles[0] if usable_oracles else []
    oracle_record = oracle_records[0] if oracle_records else {}

    # The path-map strategy is strictly ADDITIVE: it must never break run_loop. The
    # clean decline is FieldmapProposalError (no JSON island / unparseable / too-weak
    # map); we also swallow any other Exception (e.g. an emit hiccup on a pathological
    # map) and return None so the value-path + LLM candidates carry on regardless.
    # BaseException (KeyboardInterrupt/SystemExit) is intentionally NOT caught.
    try:
        fieldmap = await propose_fieldmap(
            sample_html, oracle_record, schema_ref, completer, model=model
        )
        return emit_pathmap_crawler(
            family=family,
            schema_ref=schema_ref,
            json_markers=JSON_MARKERS,
            fieldmap=fieldmap,
        )
    except Exception:  # noqa: BLE001 — additive strategy must never break run_loop
        return None


async def run_loop(
    family: str,
    seed_urls: list[str],
    ctx: FetchContext,
    registry: Registry,
    completer: Completer,
    schema_ref: str,
    *,
    fixtures_dir: Path,
    model: str = "anthropic/claude-fable-5",
    k: int = 2,
    max_rounds: int = 3,
    n_samples: int = 3,
    min_oracles: int = 3,
    agreement_bar: float = 0.98,
    now: str | None = None,
) -> LoopResult:
    """Run the regeneration loop with TIERED MODEL ESCALATION (a loop-engineering
    technique). The first attempt regenerates with ``model``. If it cannot promote
    — the cheap model's *oracle* is too noisy for the strict gauntlet, or its
    *codegen* can't clear the bar — and a stronger model exists
    (:func:`crawloop.llm.escalation_model`), the WHOLE regeneration (oracle +
    codegen) is retried once with that stronger model. The promoted artifact is
    still free deterministic code, so the one-time stronger-model cost amortizes.
    "No samples" is never retried (sampling is HTTP, not the model's fault)."""
    common = dict(
        fixtures_dir=fixtures_dir, k=k, max_rounds=max_rounds, n_samples=n_samples,
        min_oracles=min_oracles, agreement_bar=agreement_bar, now=now,
    )
    result = await _run_loop_once(
        family, seed_urls, ctx, registry, completer, schema_ref, model=model, **common
    )
    if result.ok or result.reason == "no samples":
        return result
    stronger = escalation_model(model)
    if stronger is None:
        return result
    retried = await _run_loop_once(
        family, seed_urls, ctx, registry, completer, schema_ref, model=stronger, **common
    )
    return retried if retried.ok else result


async def _run_loop_once(
    family: str,
    seed_urls: list[str],
    ctx: FetchContext,
    registry: Registry,
    completer: Completer,
    schema_ref: str,
    *,
    fixtures_dir: Path,
    model: str = "anthropic/claude-fable-5",
    k: int = 2,
    max_rounds: int = 3,
    n_samples: int = 3,
    min_oracles: int = 3,
    agreement_bar: float = 0.98,
    now: str | None = None,
) -> LoopResult:
    """Run the regeneration loop for ``family`` and return its outcome.

    See the module docstring for the full pipeline. ``ctx`` is the injected
    :class:`~crawloop.contract.FetchContext` (sampling fetches through it).
    ``schema_ref`` is the family's target schema. ``fixtures_dir`` roots the
    golden fixtures loaded for the gauntlet and refreshed on promote.
    ``model``/``k``/``max_rounds``/``n_samples`` tune codegen + sampling;
    ``min_oracles`` (default 3) is the floor on usable oracles below which the run
    escalates rather than promote against too thin a ground truth (§2/§9/§15);
    ``now`` pins timestamps for deterministic tests.

    New-family bootstrap is NOT a special case: when nothing is registered for
    ``family`` yet, ``load_fixtures`` returns ``[]`` and ``active_source``
    returns ``None``, so the same path generates a first version from scratch and
    promotes it.

    Returns a :class:`LoopResult`: ``ok`` with the promoted version, or an
    escalation (no samples / oracle failed / max rounds exhausted).
    """
    # 1) SAMPLE.
    samples = await collect_samples(seed_urls, ctx, n=n_samples)
    if not samples:
        return _escalate(
            registry, family, schema_ref, reason="no samples", rounds=0, now=now,
            data={"seed_urls": seed_urls},
        )

    # 2) ORACLE: direct_extract per sample; drop a sample whose oracle fails so
    #    we never generate against a bad ground truth.
    usable_samples: list[tuple[str, str]] = []
    usable_oracles: list[list[dict]] = []
    for url, html in samples:
        try:
            oracle = await direct_extract(html, schema_ref, completer, model=model, source_url=url)
        except ExtractionFailed:
            continue  # bad ground truth for this page -> drop it
        usable_samples.append((url, html))
        usable_oracles.append(oracle)

    # The LLM oracle can itself be wrong on any single page; design §2/§9/§15 bound
    # that error by requiring agreement across >= min_oracles independent samples.
    # Fewer than that (after dropping failed oracles) is too thin a ground truth to
    # promote against — escalate rather than risk crowning a wrong crawler. This
    # subsumes the "zero usable oracles" case (got 0, need min_oracles).
    if len(usable_oracles) < min_oracles:
        return _escalate(
            registry, family, schema_ref,
            reason=f"insufficient oracles: got {len(usable_oracles)}, need {min_oracles}",
            rounds=0, now=now,
            data={"samples": len(samples), "usable_oracles": len(usable_oracles),
                  "min_oracles": min_oracles},
        )

    # 3) CONTEXT: golden fixtures for the regression gate, and the version we are
    #    replacing (None for a brand-new family — same code path).
    fixtures = load_fixtures(fixtures_dir, family)
    prev_source = registry.active_source(family)

    # 3b) DETERMINISTIC VALUE-PATH CANDIDATE (first-class, no LLM). When the samples
    #     embed the record as a JSON island, discover where each oracle value lives
    #     and emit a crawler that reads those exact paths — reliable + free. It is
    #     fed to the FIRST round's gauntlet ALONGSIDE the LLM candidates and, being
    #     a candidate like any other, is promoted only if it clears every gate; when
    #     it doesn't (or doesn't apply), the LLM codegen rounds carry on as before.
    value_path_source = _value_path_candidate(
        family, usable_samples, usable_oracles, schema_ref
    )

    # 3c) DETERMINISTIC PATH-MAP CANDIDATE (first-class, ONE LLM call). The value-path
    #     candidate above finds only fields whose oracle value appears VERBATIM in the
    #     JSON; it misses NORMALIZED fields (a unit-scaled number, an enum code the JSON
    #     stores as a source label, a concatenated location). For those we ask the
    #     model ONCE to propose a declarative {field: {path, transform}} map and emit a
    #     free, deterministic crawler that executes it. Like the value-path candidate
    #     it joins the FIRST round's gauntlet and is promoted only if it clears every
    #     gate; if the strategy doesn't apply (no JSON island) or the proposal fails,
    #     `_path_map_candidate` returns None and the other candidates carry on.
    path_map_source = await _path_map_candidate(
        family, usable_samples, usable_oracles, schema_ref, completer, model
    )

    # 4) ROUNDS: codegen -> gauntlet -> promote, carrying a failure report.
    failure_report: str | None = None
    for round_no in range(1, max_rounds + 1):
        candidates = await generate_candidates(
            usable_samples, usable_oracles, schema_ref, prev_source,
            failure_report, completer, model=model, k=k,
        )
        # Deterministic candidates lead round 1 only (fixed output; FIRST so a free
        # deterministic crawler wins ties in run_gauntlet's max()).
        if round_no == 1:
            deterministic = [
                src for src in (value_path_source, path_map_source) if src is not None
            ]
            candidates = [*deterministic, *candidates]
        best, all_scores = run_gauntlet(
            candidates, usable_samples, usable_oracles, fixtures, schema_ref,
            agreement_bar=agreement_bar,
        )
        if best is not None:
            # Gate 5 (history cross-check): record large volatile-field moves in the
            # promote audit. Advisory only — never blocks promotion.
            warnings = _history_warnings(
                registry, family, best.source, usable_samples[0], schema_ref
            )
            # Hybrid residual set: fields this winner leaves blank vs the oracle,
            # persisted so the runtime tail-fills just those ([] => zero LLM calls).
            residual = _residual_fields(
                best.source, usable_samples, usable_oracles, schema_ref
            )
            version = promote(
                registry, family, best.source, usable_samples, usable_oracles,
                schema_ref, fixtures_dir=fixtures_dir,
                scores=_score_summary(best), history_warnings=warnings,
                residual_fields=residual, now=now,
            )
            return LoopResult(
                ok=True, version=version, rounds=round_no, escalated=False,
                reason="promoted",
            )
        # No winner this round: summarise why for the next prompt and retry.
        failure_report = _failure_report(round_no, all_scores)

    # 5) EXHAUSTED.
    return _escalate(
        registry, family, schema_ref, reason="max rounds exhausted", rounds=max_rounds,
        now=now, data={"rounds": max_rounds},
    )
