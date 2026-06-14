"""The Loop's PROMOTE step (Task 9.5): register a winner, refresh its fixtures.

When the gauntlet (Task 9.4) crowns a passing candidate, :func:`promote` is the
ONE place that makes it the family's live crawler and records why. It is a thin
composition over the registry's gated primitives — it never writes a version file
or an audit row itself, it calls :meth:`Registry.add_version` (which AST-gates
before writing), :meth:`Registry.set_active`, and :meth:`Registry.write_audit`.
Alongside that it persists the sample pages + their oracle JSON as the family's
golden FIXTURES, so the next regeneration round can check the new code against the
pages this one was accepted on (the gauntlet's Gate-4 regression input).

Fixtures live under ``fixtures_dir/<family_dir(family)>/sample_<i>.html`` with a
matching ``sample_<i>.json`` (the oracle's records for that page). The directory
name is :func:`~crawloop.registry.family_dir`, the same injective mapping the
registry uses for code files, so two families can never share a fixture dir.
:func:`load_fixtures` reads them back as ``(html, expected_items)`` pairs (the
shape the gauntlet wants); :func:`save_fixtures` writes/refreshes them.
"""

from __future__ import annotations

import json
from pathlib import Path

from crawloop.registry import Registry, family_dir


def _family_fixture_dir(fixtures_dir: Path, family: str) -> Path:
    """The fixture directory for ``family`` under ``fixtures_dir``.

    Uses :func:`~crawloop.registry.family_dir` (the injective slug+hash) so
    distinct families never collide onto one directory — the same guarantee the
    registry gives code files.
    """
    return Path(fixtures_dir) / family_dir(family)


def load_fixtures(fixtures_dir: Path, family: str) -> list[tuple[str, list[dict]]]:
    """Load ``family``'s stored golden fixtures as ``(html, expected_items)``.

    Reads every ``sample_*.html`` in the family's fixture dir together with its
    matching ``sample_*.json`` (the oracle records that page was accepted on),
    sorted by filename for deterministic order. Returns ``[]`` if the family has
    no fixture dir yet (a brand-new family). An ``.html`` with no matching
    ``.json`` is skipped (an incomplete pair is not a usable fixture).
    """
    fixture_dir = _family_fixture_dir(fixtures_dir, family)
    if not fixture_dir.is_dir():
        return []
    out: list[tuple[str, list[dict]]] = []
    for html_path in sorted(fixture_dir.glob("sample_*.html")):
        json_path = html_path.with_suffix(".json")
        if not json_path.is_file():
            continue
        html = html_path.read_text(encoding="utf-8")
        expected = json.loads(json_path.read_text(encoding="utf-8"))
        out.append((html, expected))
    return out


def save_fixtures(
    fixtures_dir: Path,
    family: str,
    samples: list[tuple[str, str]],
    oracle_jsons: list[list[dict]],
) -> None:
    """Write/refresh ``family``'s fixtures from the promoted ``samples`` + oracle.

    For each ``(url, html)`` sample and its aligned oracle records, writes
    ``sample_<i>.html`` and ``sample_<i>.json`` into the family's fixture dir
    (created if needed). ``default=str`` keeps non-JSON-native oracle values
    (e.g. ``Decimal`` prices) serialisable. Existing files for the same index are
    overwritten so a fresh promotion REPLACES the golden set with the pages this
    version was actually accepted on, rather than accumulating stale ones.
    """
    fixture_dir = _family_fixture_dir(fixtures_dir, family)
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for i, ((_url, html), records) in enumerate(zip(samples, oracle_jsons)):
        (fixture_dir / f"sample_{i}.html").write_text(html, encoding="utf-8")
        (fixture_dir / f"sample_{i}.json").write_text(
            json.dumps(records, default=str), encoding="utf-8"
        )


def promote(
    registry: Registry,
    family: str,
    source: str,
    samples: list[tuple[str, str]],
    oracle_jsons: list[list[dict]],
    schema_ref: str,
    *,
    fixtures_dir: Path,
    scores: object | None = None,
    history_warnings: list[str] | None = None,
    residual_fields: list[str] | None = None,
    now: str | None = None,
) -> int:
    """Make ``source`` the family's active version, refresh fixtures, audit it.

    Steps, in order:

    1. ``n = registry.add_version(family, source, now=now)`` — the registry
       AST-gates the source before writing it to disk (ungated code never lands)
       and returns the new version number.
    2. ``registry.set_active(family, n)`` — promote it to the active rung.
    3. :func:`save_fixtures` — persist the samples + oracle JSON as the family's
       golden fixtures, so the next round can regression-check against them.
    4. ``registry.write_audit("promote", ...)`` — record ``to_version``,
       ``schema_ref``, the (already-serialisable) ``scores`` summary, the Gate-5
       ``history_warnings`` (an advisory list of large volatile-field moves vs the
       family's recent history; ``[]`` when none / no prior history), and the
       hybrid ``residual_fields`` (the fields this deterministic winner
       systematically leaves blank vs the oracle — the runtime tail-fill set; ``[]``
       when the crawler is complete, which means ZERO LLM calls at runtime).

    Returns the new active version number. ``scores`` is an optional
    JSON-serialisable summary of the winning candidate's gauntlet score; it is
    stored verbatim in the audit ``data`` (the caller passes something already
    serialisable — e.g. the winning :class:`CandidateScore`'s fields as a dict).
    ``history_warnings`` is the Gate-5 cross-check result for the promoted
    extraction (recorded for observability; it does NOT block promotion — Gate 5
    is non-fatal at runtime per the design). ``residual_fields`` is the hybrid's
    per-version residual set, persisted here in the SAME promote audit ``data`` the
    registry already writes (no new column) and read back at runtime by
    :meth:`~crawloop.registry.Registry.active_residual_fields`.
    """
    n = registry.add_version(family, source, now=now)
    registry.set_active(family, n)
    save_fixtures(fixtures_dir, family, samples, oracle_jsons)
    registry.write_audit(
        "promote",
        family=family,
        data={
            "to_version": n,
            "schema_ref": schema_ref,
            "scores": scores,
            "history_warnings": history_warnings or [],
            "residual_fields": residual_fields or [],
        },
        now=now,
    )
    return n
