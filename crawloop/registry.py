"""The registry: SQLite metadata + on-disk crawler files + the version ladder.

This is the system's source of truth for "which crawlers exist, which version is
live, and what happened". It owns three things:

* a small SQLite database (schema in :mod:`migrations.sql`) holding families,
  their version ladders, the audit trail, the per-domain access store, and run
  history;
* the on-disk crawler code files under ``crawlers_dir/<family_dir(family)>/v<n>.py``
  (``family_dir`` is injective; ``slug`` is the readable label it builds on);
* the gated path that turns LLM-generated source into a registered, loadable
  version.

Security posture (the reason this module exists as a trust boundary):

* :meth:`Registry.add_version` runs the AST gate (:func:`safety.ast_check` with
  ``raise_on_violation=True``) **before** it writes anything to disk, so ungated
  source is never persisted, and records ``source_sha = sha256(source)``.
* :meth:`Registry.load_crawler` reads the on-disk file **exactly once** and gates,
  integrity-checks, compiles, and executes that single in-memory string — the
  importer is never allowed a second disk read. So the bytes that were AST-gated
  are the exact bytes that run (no swap window), and the same bytes are re-hashed
  against ``source_sha`` to detect any post-registration tampering
  (:class:`ASTViolation` for a banned construct, :class:`IntegrityError` for
  byte-level drift).
* Every SQL statement binds its values with ``?`` placeholders. Identifiers and
  values are NEVER formatted into the SQL string — there is no f-string / ``%``
  interpolation of data anywhere in this module.

Determinism: any method that stamps a timestamp accepts an optional ``now``
(an ISO-8601 string); when ``None`` it defaults to the current UTC time. Tests
pin ``now`` so ordering and stored values are deterministic.
"""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import re
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from crawloop.contract import Crawler
from crawloop.safety import ast_check


class IntegrityError(Exception):
    """Raised when a crawler file's on-disk bytes do not match the sha256 recorded
    at registration — i.e. the file was changed after ``add_version`` gated and
    hashed it. Distinct from :class:`~crawloop.safety.ASTViolation` (which
    fires first, for a banned *construct*); this fires for any byte-level drift,
    even an AST-clean one, so post-registration tampering is genuinely detectable.
    """

_MIGRATIONS = Path(__file__).with_name("migrations.sql")

# Monotonic counter giving every load_crawler call a unique module name, so a
# reload of the same file (e.g. after tampering) is parsed fresh from disk
# instead of being served from a cached sys.modules entry.
_load_counter = itertools.count()

# Characters allowed to survive into an on-disk path component after the family's
# structural separators ('.', '/') have been mapped. Anything else (spaces, shell
# metacharacters, '..' traversal fragments) is dropped, so a hostile family name
# can never escape ``crawlers_dir`` or inject path syntax.
_UNSAFE_SLUG_CHARS = re.compile(r"[^A-Za-z0-9_]")


def slug(family: str) -> str:
    """Filesystem-safe, human-readable label for a family. Pure function.

    ``"books.toscrape.com/product_list"`` -> ``"books_toscrape_com__product_list"``.
    Mapping: ``/`` -> ``__`` and ``.`` -> ``_`` (so the path separator is visually
    distinct from a dotted host), then every remaining character outside
    ``[A-Za-z0-9_]`` is stripped. The strip step also removes any ``.`` left by a
    ``..`` traversal attempt, so the result is always a single safe path segment.

    ``slug`` is for DISPLAY only and is intentionally NOT injective — ``"a/b"``,
    ``"a__b"``, ``"a.b"`` and ``"a_b"`` all map to ``"a_b"``. For the on-disk
    location use :func:`family_dir`, which appends a hash of the raw name so
    distinct families never share a directory. Raises :class:`ValueError` for an
    empty family or one that strips to nothing (it must never name the
    ``crawlers_dir`` root itself).
    """
    mapped = family.replace("/", "__").replace(".", "_")
    out = _UNSAFE_SLUG_CHARS.sub("", mapped)
    if not out:
        raise ValueError(f"family {family!r} has no filesystem-safe slug (empty)")
    return out


def family_dir(family: str) -> str:
    """Injective on-disk directory name for a family. Pure function.

    The readable :func:`slug` is NOT injective, so two different families can
    collide to the same slug and silently overwrite each other's ``v<n>.py``
    files (and the DB then points both families at one file). We keep the slug as
    a readable prefix for navigability but append a short, stable hash of the RAW
    family name: ``f"{slug(family)}-{sha256(family).hexdigest()[:8]}"``. The hash
    is taken over the exact original string, so distinct families differ in the
    suffix even when their slugs are identical.

    Raises :class:`ValueError` for an empty family (via :func:`slug`).
    """
    digest = hashlib.sha256(family.encode("utf-8")).hexdigest()[:8]
    return f"{slug(family)}-{digest}"


def _now_iso(now: str | None) -> str:
    """The caller-pinned timestamp, or the current UTC time as ISO-8601."""
    return now if now is not None else datetime.now(timezone.utc).isoformat()


def _ladder_entry(row: sqlite3.Row) -> dict:
    """The single place a ``versions`` row becomes a public ladder dict.

    Keeps the ladder's shape defined in exactly one spot (One purpose -> one
    place), so :meth:`Registry.version_ladder` and any future caller agree.
    """
    return {
        "n": row["n"],
        "status": row["status"],
        "path": row["path"],
        "runs": row["runs"],
        "successes": row["successes"],
    }


class Registry:
    """SQLite + on-disk files registry. See module docstring for the contract.

    Construct with an in-memory DB for tests (``db_path=":memory:"``) or a file
    path for real use, plus the directory under which crawler code files live.
    """

    def __init__(self, db_path: str = ":memory:", *, crawlers_dir: Path) -> None:
        self.crawlers_dir = Path(crawlers_dir)
        self.crawlers_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the engine (async, possibly multi-task) can
        # share one Registry; sqlite serializes writes internally for our usage.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_MIGRATIONS.read_text())
        self._conn.commit()

    # -- families ---------------------------------------------------------- #

    def upsert_family(
        self, family: str, url_patterns: list[str], schema_ref: str, *, now: str | None = None
    ) -> None:
        """Insert or update a family row. ``url_patterns`` is stored as JSON text.

        On conflict the patterns and schema_ref are updated in place; the original
        ``created_at`` and ``status`` are preserved (only set on first insert).
        """
        self._conn.execute(
            """
            INSERT INTO families (family, url_patterns, schema_ref, status, created_at)
            VALUES (?, ?, ?, 'healthy', ?)
            ON CONFLICT(family) DO UPDATE SET
                url_patterns = excluded.url_patterns,
                schema_ref   = excluded.schema_ref
            """,
            (family, json.dumps(url_patterns), schema_ref, _now_iso(now)),
        )
        self._conn.commit()

    def get_family(self, family: str) -> dict | None:
        """The family row as a dict (``url_patterns`` decoded back to a list), or
        ``None`` if the family is unknown."""
        row = self._conn.execute(
            "SELECT * FROM families WHERE family = ?", (family,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["url_patterns"] = json.loads(out["url_patterns"]) if out["url_patterns"] else []
        return out

    def all_families(self) -> list[dict]:
        """Every family row as a dict, sorted by ``family`` name for determinism.

        Same per-row shape as :meth:`get_family` (``url_patterns`` decoded back to
        a list). The deterministic name ordering is what lets the M10 router pick
        a stable "first match wins" family when a URL is covered by more than one
        family's patterns.
        """
        rows = self._conn.execute(
            "SELECT * FROM families ORDER BY family ASC"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            entry = dict(row)
            entry["url_patterns"] = (
                json.loads(entry["url_patterns"]) if entry["url_patterns"] else []
            )
            out.append(entry)
        return out

    def set_family_status(self, family: str, status: str) -> None:
        """Set a family's lifecycle ``status`` (``healthy`` | ``degraded`` |
        ``regenerating`` | ``escalated``).

        Used by the M9 Loop to mark a family ``escalated`` when regeneration
        gives up (no usable oracle, or max rounds exhausted). Raises
        :class:`LookupError` if the family has no row — the families row must
        already exist (``add_version`` auto-creates it), so setting status on an
        unknown family is a logic error rather than a silent no-op. This is the
        FAMILY-level analogue of :meth:`mark_domain_status` (which is per-domain).
        """
        cursor = self._conn.execute(
            "UPDATE families SET status = ? WHERE family = ?",
            (status, family),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"no family row for {family!r}")
        self._conn.commit()

    # -- versions ---------------------------------------------------------- #

    def add_version(self, family: str, source: str, *, now: str | None = None) -> int:
        """Register a new crawler version for ``family`` and return its number.

        Steps, in this exact order:

        1. **AST gate FIRST.** ``ast_check(source, raise_on_violation=True)`` — if
           the source is outside the allowlist this raises :class:`ASTViolation`
           and NOTHING is written to disk or the DB.
        2. Compute the next ``n`` as ``max(existing n) + 1`` (or ``1``).
        3. Write the source to ``crawlers_dir/<family_dir(family)>/v<n>.py``
           (creating the family directory if needed). ``family_dir`` is injective
           so two families can never collide onto one file.
        4. Insert the ``versions`` row with status ``'fallback'`` (NOT active —
           promotion to active is an explicit :meth:`set_active` / Loop step),
           the file path, and ``source_sha = sha256(source.encode("utf-8"))``.

        If ``family`` has no row yet, a minimal family row is auto-created first
        (documented behavior) so the Loop can bootstrap a brand-new family with a
        single call.
        """
        # 1) Gate before any side effect. Ungated code must never reach disk.
        ast_check(source, raise_on_violation=True)

        # Auto-create a minimal family row if absent (FK target + bootstrap path).
        if self.get_family(family) is None:
            self.upsert_family(family, [], "", now=now)

        # 2) Next version number for this family.
        n = self._next_version(family)

        # 3) Write the immutable code artifact under the INJECTIVE family dir
        #    (slug is for display only and can collide across families). Pin
        #    utf-8 on write so the bytes hashed here are the bytes load reads back.
        dir_path = self.crawlers_dir / family_dir(family)
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"v{n}.py"
        path.write_text(source, encoding="utf-8")

        # 4) Record it as a fallback (not yet active). The sha is over the exact
        #    utf-8 bytes written, so load_crawler can verify the file is unchanged.
        source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self._conn.execute(
            """
            INSERT INTO versions (family, n, status, path, source_sha, promoted_at)
            VALUES (?, ?, 'fallback', ?, ?, ?)
            """,
            (family, n, str(path), source_sha, _now_iso(now)),
        )
        self._conn.commit()
        return n

    def _next_version(self, family: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(n) AS max_n FROM versions WHERE family = ?", (family,)
        ).fetchone()
        return (row["max_n"] or 0) + 1

    def set_active(self, family: str, n: int) -> None:
        """Make version ``n`` the active one and demote the previously-active one
        to ``'fallback'``. Other versions (including ``'archived'`` ones) are left
        untouched. This is the ladder's append+reorder promotion (design §7).

        Verifies ``(family, n)`` exists FIRST and raises :class:`LookupError`
        without changing anything if it does not — otherwise demoting the current
        active version while promoting a non-existent one would strand the family
        with zero active versions. The demote+promote pair runs in a single
        transaction (``with self._conn``) so a mid-operation failure cannot leave
        the family with no active version either.
        """
        if self._versions_row(family, n) is None:
            raise LookupError(f"no version {n} for family {family!r}")
        # One transaction: demote the current active (never touch archived ones),
        # then promote n. Either both land or neither does.
        with self._conn:
            self._conn.execute(
                "UPDATE versions SET status = 'fallback' "
                "WHERE family = ? AND status = 'active'",
                (family,),
            )
            self._conn.execute(
                "UPDATE versions SET status = 'active' WHERE family = ? AND n = ?",
                (family, n),
            )

    def version_ladder(self, family: str) -> list[dict]:
        """The family's versions as ladder dicts, ordered the way the runtime walks
        them: the ``active`` version first, then every other version by ``n``
        descending (newest fallbacks before older ones, archived last by virtue of
        their lower ``n`` only if any — status is included so callers can filter).

        Ordering is done in SQL: ``status = 'active'`` sorts before everything
        else, then ``n DESC``. Empty list for an unknown/empty family.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM versions
            WHERE family = ?
            ORDER BY (status = 'active') DESC, n DESC
            """,
            (family,),
        ).fetchall()
        return [_ladder_entry(r) for r in rows]

    def record_run(self, family: str, n: int, ok: bool) -> None:
        """Increment version ``n``'s ``runs``; also ``successes`` when ``ok``.

        ``ok`` is bound as an int (1/0) so the SQL stays parameterized. Raises
        :class:`LookupError` if no row matched ``(family, n)`` (checked via the
        cursor's ``rowcount``) — recording a run against a version that does not
        exist is a logic error, not a silent no-op.
        """
        cursor = self._conn.execute(
            "UPDATE versions SET runs = runs + 1, successes = successes + ? "
            "WHERE family = ? AND n = ?",
            (1 if ok else 0, family, n),
        )
        if cursor.rowcount == 0:
            raise LookupError(f"no version {n} for family {family!r}")
        self._conn.commit()

    def rollback(self, family: str) -> int:
        """Roll the active version back one rung and return the new active ``n``.

        Demotes the current active version to ``'archived'`` and promotes the
        most-recent **non-archived** fallback (highest ``n`` among ``'fallback'``)
        to active. Raises :class:`LookupError` if there is no fallback to promote
        (nothing safe to roll back to)."""
        target = self._conn.execute(
            "SELECT n FROM versions WHERE family = ? AND status = 'fallback' "
            "ORDER BY n DESC LIMIT 1",
            (family,),
        ).fetchone()
        if target is None:
            raise LookupError(f"no fallback version to roll back to for family {family!r}")
        # Archive the (bad) current active, then promote the chosen fallback.
        self._conn.execute(
            "UPDATE versions SET status = 'archived' "
            "WHERE family = ? AND status = 'active'",
            (family,),
        )
        new_active = target["n"]
        self._conn.execute(
            "UPDATE versions SET status = 'active' WHERE family = ? AND n = ?",
            (family, new_active),
        )
        self._conn.commit()
        return int(new_active)

    # -- gated loader (Task 5.2) ------------------------------------------- #

    def load_crawler(self, family: str, n: int | None = None) -> Crawler:
        """Load a registered version's on-disk file as a live :class:`Crawler`.

        Resolve the version (``n`` if given, else the family's ``active`` one) and
        read its file **exactly once** into memory. The same in-memory string is
        then gated, integrity-checked, compiled, and executed — the importer is
        never allowed to re-read disk. This closes a TOCTOU window: if the file
        were read once for the gate and again by the importer, a swap between the
        two reads would execute UNGATED bytes. Here the gated bytes ARE the
        executed bytes.

        Order, on the single in-memory ``source``:

        1. **AST gate** (``ast_check(..., raise_on_violation=True)``) — a banned
           construct (e.g. an appended ``import os``) raises
           :class:`~crawloop.safety.ASTViolation` and nothing is compiled.
        2. **Integrity check** — ``sha256(source)`` must equal the ``source_sha``
           recorded at :meth:`add_version`. Any byte-level drift since
           registration (even an AST-clean comment edit) raises
           :class:`IntegrityError`. The gate runs first, so a *malicious* tamper
           surfaces as ``ASTViolation`` before we reach this check.
        3. **Compile + exec** the gated source in a throwaway module namespace
           under a process-unique name (registered in ``sys.modules`` only for the
           duration of ``exec`` so module-level machinery works, then removed).

        Re-checking on load is deliberate: ``add_version`` gated and hashed the
        source before writing, but the file is the real trust surface at load
        time. Both the construct gate and the byte integrity check run here.

        Raises :class:`LookupError` if the family/version does not exist or no
        active version is set when ``n`` is ``None``.
        """
        row = self._resolve_version(family, n)
        path = Path(row["path"])
        # SINGLE disk read. Pin utf-8 so decoding matches add_version's write and
        # ignores any PEP 263 coding cookie an importer would otherwise honor —
        # the gated bytes and the executed bytes decode identically.
        source = path.read_text(encoding="utf-8")

        # 1) Gate the exact bytes we will run — tampering with a banned construct
        #    is caught here, before compile/exec.
        ast_check(source, raise_on_violation=True)

        # 2) Defense-in-depth: the gated bytes must match what was registered.
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != row["source_sha"]:
            raise IntegrityError(
                f"{family} v{row['n']}: on-disk source does not match registered sha"
            )

        # 3) Compile and execute the SAME in-memory string (no second disk read).
        code = compile(source, str(path), "exec")
        mod_name = f"_crawler_{slug(family)}_v{row['n']}_{next(_load_counter)}"
        module = types.ModuleType(mod_name)
        module.__file__ = str(path)
        # Register so module-level machinery (dataclasses, super(), etc.) resolves,
        # then remove it in finally so a process-unique reload is never cached.
        sys.modules[mod_name] = module
        try:
            exec(code, module.__dict__)
        finally:
            sys.modules.pop(mod_name, None)

        crawler_cls = self._find_crawler_class(module)
        instance = crawler_cls()
        # Final structural confirmation that what we built satisfies the contract
        # (isinstance works on the runtime_checkable Protocol; issubclass does not
        # because Crawler has non-method members).
        if not isinstance(instance, Crawler):
            raise LookupError(
                f"loaded class {crawler_cls.__name__!r} from {path} is not a Crawler"
            )
        return instance

    def active_source(self, family: str) -> str | None:
        """The SOURCE STRING of ``family``'s active version, or ``None``.

        The M9 codegen step wants the source of the version it is replacing
        (``prev_source``) so a regeneration can build on it. This reads the
        active version's on-disk file and returns its text; it returns ``None``
        when the family has no active version (unknown family, or versions
        registered but never promoted), so a brand-new family bootstraps with
        ``prev_source=None`` down the same code path.

        Unlike :meth:`load_crawler`, this does NOT gate/compile/exec — it is just
        the bytes, for prompting. (``load_crawler`` remains the only path that
        executes a version, and it re-gates + integrity-checks before doing so.)
        """
        row = self._conn.execute(
            "SELECT path FROM versions WHERE family = ? AND status = 'active'",
            (family,),
        ).fetchone()
        if row is None:
            return None
        return Path(row["path"]).read_text(encoding="utf-8")

    def active_residual_fields(self, family: str) -> list[str]:
        """The hybrid RESIDUAL SET persisted for ``family``'s active version, or ``[]``.

        The deterministic crawler the Loop promotes is exact + free but systematically
        leaves a few NORMALIZED/INFERRED fields blank on wide schemas; that "residual
        set" is computed at promote time (:func:`crawloop.hybrid.compute_residual\
        _fields`) and stored in the version's ``promote`` audit ``data`` under
        ``residual_fields`` (the existing per-version metadata the promote step
        already writes — no new column). This reads it back for the runtime hybrid.

        Resolution: find the family's ACTIVE version number, then the NEWEST ``promote``
        audit row for this family whose ``data.to_version`` is that version, and return
        its ``residual_fields``. Following the active version (rather than the newest
        promote) means a rollback/repromote serves the residual set of whatever version
        is live now. Returns ``[]`` — the safe, no-LLM default — when the family is
        unknown, has no active version, has no matching promote audit, or that audit
        predates the hybrid (legacy rows carry no ``residual_fields`` key).
        """
        active = self._conn.execute(
            "SELECT n FROM versions WHERE family = ? AND status = 'active'",
            (family,),
        ).fetchone()
        if active is None:
            return []
        active_n = active["n"]
        # Newest promote audit for this family (id DESC) whose to_version is the active
        # one. Scanning newest-first means a re-promote of the same version wins with
        # its latest residual set. Parsed lazily so only the matching row is decoded.
        rows = self._conn.execute(
            "SELECT data FROM audit WHERE family = ? AND event = 'promote' "
            "ORDER BY id DESC",
            (family,),
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"]) if row["data"] else {}
            if data.get("to_version") == active_n:
                residual = data.get("residual_fields", [])
                return list(residual) if residual else []
        return []

    def _resolve_version(self, family: str, n: int | None) -> sqlite3.Row:
        """The ``versions`` row to load: version ``n``, or the active one if
        ``n`` is ``None``. Raises :class:`LookupError` if there is no such row."""
        if n is None:
            row = self._conn.execute(
                "SELECT * FROM versions WHERE family = ? AND status = 'active'",
                (family,),
            ).fetchone()
            if row is None:
                raise LookupError(f"no active version for family {family!r}")
            return row
        row = self._versions_row(family, n)
        if row is None:
            raise LookupError(f"no version {n} for family {family!r}")
        return row

    @staticmethod
    def _find_crawler_class(module: types.ModuleType) -> type:
        """The single concrete crawler class DEFINED IN ``module``.

        ``module`` is the throwaway namespace ``load_crawler`` exec'd the gated
        source into. We look for classes whose ``__module__`` is this loaded
        module (so the imported ``Crawler`` Protocol / ``CrawlResult`` from
        :mod:`crawloop.contract` are excluded) that carry the contract's
        surface: ``family``, ``schema_ref`` and an ``async def crawl``.
        ``issubclass(cls, Crawler)`` can't be used — ``Crawler`` is a Protocol
        with non-method members — so we match structurally here and confirm with
        ``isinstance`` on the instance in :meth:`load_crawler`. Exactly one match
        is required; zero or many is a clear error.
        """
        candidates = [
            obj
            for obj in module.__dict__.values()
            if isinstance(obj, type)
            and obj.__module__ == module.__name__
            and hasattr(obj, "family")
            and hasattr(obj, "schema_ref")
            and inspect.iscoroutinefunction(getattr(obj, "crawl", None))
        ]
        if len(candidates) != 1:
            raise LookupError(
                f"expected exactly one Crawler class in {module.__name__}, "
                f"found {len(candidates)}"
            )
        return candidates[0]

    # -- audit log (Task 5.3) ---------------------------------------------- #

    def write_audit(
        self, event: str, *, family: str | None = None, data: dict, now: str | None = None
    ) -> None:
        """Append an audit entry to the ``audit`` table AND mirror it as one JSON
        line to ``crawlers_dir/audit.jsonl``.

        The entry shape mirrors design §9: ``ts``, ``event``, ``family``,
        ``data`` (an event-specific JSON object). ``data`` is JSON-encoded once
        and used for both the row and the mirror so the two never diverge.
        """
        ts = _now_iso(now)
        data_json = json.dumps(data)
        self._conn.execute(
            "INSERT INTO audit (ts, family, event, data) VALUES (?, ?, ?, ?)",
            (ts, family, event, data_json),
        )
        self._conn.commit()
        # Mirror: append-only, one JSON object per line. Built from the same
        # values + already-encoded data so the row and the line agree exactly.
        line = json.dumps({"ts": ts, "event": event, "family": family, "data": data})
        with self._audit_jsonl.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_audit(self, family: str | None = None) -> list[dict]:
        """Audit entries newest-first (``id`` descending), optionally filtered to
        one ``family``. ``data`` is decoded back to a dict on the way out."""
        if family is None:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY id DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE family = ? ORDER BY id DESC", (family,)
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["data"] = json.loads(entry["data"]) if entry["data"] else {}
            out.append(entry)
        return out

    @property
    def _audit_jsonl(self) -> Path:
        return self.crawlers_dir / "audit.jsonl"

    # -- run history (Task 6.2) -------------------------------------------- #
    #
    # The per-URL extraction history the M6 executor writes after a version
    # validates, and the M7 history gate reads back. The registry owns it so all
    # persistence lives in one place.

    def record_history(
        self, family: str, url: str, version: int, items: list[dict], *, now: str | None = None
    ) -> None:
        """Append one extraction to ``run_history``.

        ``items`` is JSON-encoded with ``default=str`` so coerced values the
        executor produces (e.g. ``Decimal`` prices) serialize instead of raising.
        Values bind with ``?`` placeholders like every statement in this module.
        """
        self._conn.execute(
            "INSERT INTO run_history (family, url, version, extracted_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (family, url, version, json.dumps(items, default=str), _now_iso(now)),
        )
        self._conn.commit()

    def recent_history(
        self, family: str, url: str | None = None, limit: int = 20
    ) -> list[dict]:
        """Recent extraction rows for ``family`` (optionally one ``url``), newest
        first, capped at ``limit``.

        Ordered by ``id`` descending (the autoincrement insertion order), so the
        newest row leads regardless of timestamp ties. ``extracted_json`` is
        parsed back into the ``items`` list on the way out. ``limit`` is bound as
        a parameter, never formatted into the SQL.
        """
        if url is None:
            rows = self._conn.execute(
                "SELECT * FROM run_history WHERE family = ? ORDER BY id DESC LIMIT ?",
                (family, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM run_history WHERE family = ? AND url = ? "
                "ORDER BY id DESC LIMIT ?",
                (family, url, limit),
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["items"] = json.loads(entry["extracted_json"]) if entry["extracted_json"] else []
            out.append(entry)
        return out

    # -- AccessStore implementation (Task 5.3) ----------------------------- #
    #
    # These three methods make the Registry satisfy access.AccessStore, so the
    # M4 recovery loop can persist/reuse winning fetch strategies through the real
    # registry instead of an in-memory fake. The Protocol is runtime_checkable, so
    # isinstance(registry, AccessStore) holds purely on these method names.

    def get_working_strategy(self, domain: str) -> str | None:
        """The persisted working strategy for ``domain``, or ``None`` if unknown."""
        row = self._domain_access_row(domain)
        return row["working_strategy"] if row is not None else None

    def set_working_strategy(
        self, domain: str, strategy: str, *, now: str | None = None
    ) -> None:
        """Upsert ``domain``'s working strategy (and bump ``updated_at``).

        Leaves any existing ``status`` untouched on conflict — only the strategy
        and timestamp change.
        """
        self._conn.execute(
            """
            INSERT INTO domain_access (domain, working_strategy, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                working_strategy = excluded.working_strategy,
                updated_at       = excluded.updated_at
            """,
            (domain, strategy, _now_iso(now)),
        )
        self._conn.commit()

    def mark_domain_status(
        self, domain: str, status: str, *, now: str | None = None
    ) -> None:
        """Upsert ``domain``'s status (and bump ``updated_at``).

        Leaves any existing ``working_strategy`` untouched on conflict — marking a
        domain blocked/healthy must not erase the strategy already known to work.
        """
        self._conn.execute(
            """
            INSERT INTO domain_access (domain, status, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                status     = excluded.status,
                updated_at = excluded.updated_at
            """,
            (domain, status, _now_iso(now)),
        )
        self._conn.commit()

    def access_rows(self) -> list[dict]:
        """Every ``domain_access`` row as a dict, sorted by ``domain``.

        A read-only view of the persistent access store for operators (the M11
        ``access status`` CLI command): each row carries the domain, its known
        ``working_strategy`` (the last strategy recovery proved works, or
        ``None``), its last-seen ``status`` (e.g. ``"escalated"``), and
        ``updated_at``. Sorted by domain so the listing is deterministic. Unlike
        :meth:`get_working_strategy` this exposes the whole row, not just the
        strategy, and unlike the ``_domain_access_row`` test accessor it returns
        all rows for display rather than one.
        """
        rows = self._conn.execute(
            "SELECT * FROM domain_access ORDER BY domain ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- test/support accessors (raw row reads; not part of the public API) - #

    def _domain_access_row(self, domain: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM domain_access WHERE domain = ?", (domain,)
        ).fetchone()

    def _versions_row(self, family: str, n: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM versions WHERE family = ? AND n = ?", (family, n)
        ).fetchone()

    def _set_status(self, family: str, n: int, status: str) -> None:
        """Set a version's status directly. Used by tests to arrange archived
        versions; production status changes go through set_active / rollback."""
        self._conn.execute(
            "UPDATE versions SET status = ? WHERE family = ? AND n = ?",
            (status, family, n),
        )
        self._conn.commit()
