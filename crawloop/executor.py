"""The executor: the pagination driver and the runtime version-ladder walk.

Two layers, both kept deliberately thin:

* :func:`run_version` — drives ONE crawler version across its paginated listing.
  Pagination lives here, not inside generated crawler code, so the page cap and
  (via the injected :class:`~crawloop.contract.FetchContext`) the central
  rate-limit / allowlist gate apply uniformly to every version (design §5). A
  crawler reports only its single page's items plus a ``next_url``; the executor
  decides whether to follow it.

* :func:`run_family` — walks a family's version ladder at runtime (active first,
  then fallbacks newest-first), running each version through :func:`run_version`
  and validating its output via an injected ``validate`` callable. The first
  version whose extraction validates wins; its run + history are recorded and,
  on the configured cadence, its fetched HTML is snapshotted. If no version
  validates, :class:`AllVersionsFailed` is raised carrying the last failing
  report for the M8 classifier.

Dependency inversion: the validator is M7 and does not exist yet, so the
executor depends only on the tiny structural :class:`ValidationLike` Protocol and
takes ``validate`` as a CALLABLE the caller injects. M7's real ValidationReport
will satisfy ``ValidationLike`` (it has ``.ok`` and a ``.reason``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from crawloop.contract import Crawler, FetchContext
from crawloop.registry import family_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from crawloop.registry import Registry


@runtime_checkable
class ValidationLike(Protocol):
    """The minimal shape the executor needs from a validation result.

    The real validator is M7; the executor never imports it. It only reads
    ``.ok`` (did the extraction pass) and ``.reason`` (why not, for the error it
    raises and for the M8 classifier). M7's ValidationReport satisfies this.
    """

    ok: bool
    reason: str


class AllVersionsFailed(Exception):
    """Raised by :func:`run_family` when every version on the ladder was tried and
    none produced a validating extraction.

    Carries enough for the M8 failure classifier to act without re-running:
    ``family`` (which family exhausted), ``reason`` (a human summary), and
    ``last_report`` — the :class:`ValidationLike` of the LAST version tried (its
    ``.ok`` is ``False`` and its ``.reason`` says why), so the classifier can read
    the concrete failure of the final rung.
    """

    def __init__(self, *, family: str, reason: str, last_report: ValidationLike | None) -> None:
        self.family = family
        self.reason = reason
        self.last_report = last_report
        super().__init__(reason)


@dataclass
class FamilyRunResult:
    """The outcome of a successful :func:`run_family` walk.

    ``used_version`` is the ladder rung whose extraction validated (active first,
    then fallbacks newest-first); ``items`` is its accumulated extraction across
    all paginated pages; ``pages_fetched`` is how many pages that version walked.
    """

    items: list[dict]
    used_version: int
    pages_fetched: int


class RecordingFetchContext:
    """A :class:`~crawloop.contract.FetchContext` decorator that records every
    ``(url, html)`` it returns from :meth:`fetch` / :meth:`fetch_rendered`.

    It WRAPS any context (it does not touch :class:`RealFetchContext`, so M4 stays
    untouched) and delegates the coercion helpers straight through. The executor
    wraps the caller's context in a fresh recorder per version attempt so it can
    snapshot exactly the HTML that version saw — without the crawler or the access
    layer knowing anything is being captured.
    """

    def __init__(self, inner: FetchContext) -> None:
        self._inner = inner
        self.pages: list[tuple[str, str]] = []

    async def fetch(self, url: str) -> str:
        html = await self._inner.fetch(url)
        self.pages.append((url, html))
        return html

    async def fetch_rendered(self, url: str, wait_for: str | None = None) -> str:
        html = await self._inner.fetch_rendered(url, wait_for)
        self.pages.append((url, html))
        return html

    def absolutize(self, base: str, href: str | None) -> str | None:
        return self._inner.absolutize(base, href)

    def parse_money(self, raw: str | None) -> Decimal | None:
        return self._inner.parse_money(raw)

    def clean_text(self, raw: str | None) -> str | None:
        return self._inner.clean_text(raw)


async def run_version(
    crawler: Crawler,
    start_url: str,
    ctx: FetchContext,
    *,
    max_pages: int = 10,
) -> tuple[list[dict], int]:
    """Drive ``crawler`` across its paginated listing starting at ``start_url``.

    Repeatedly ``await crawler.crawl(url, ctx)``, accumulating ``result.items``.
    After each page, if the crawler returned a ``next_url`` AND we have not yet
    fetched ``max_pages`` pages, follow it; otherwise stop. Returns
    ``(all_items, pages_fetched)``.

    The ``max_pages`` cap is the sole loop bound, so it also defuses a
    self-referential ``next_url`` (a page linking to itself) — the walk can never
    run longer than ``max_pages`` fetches regardless of what the site returns.
    Pagination is centralized here rather than in crawler code so the page cap
    and the injected context's rate-limit/allowlist gate apply to every version
    uniformly (design §5).
    """
    all_items: list[dict] = []
    url: str | None = start_url
    pages_fetched = 0
    while url is not None and pages_fetched < max_pages:
        result = await crawler.crawl(url, ctx)
        all_items.extend(result.items)
        pages_fetched += 1
        url = result.next_url
    return all_items, pages_fetched


def _snapshot_name(url: str) -> str:
    """A safe, collision-resistant snapshot filename for ``url``.

    A sha256 of the URL keeps the name fixed-length and free of any path syntax
    (no ``/``, ``..``, query metacharacters), so it can never escape the snapshot
    directory; same URL -> same file, so re-snapshotting a page overwrites rather
    than accreting.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest() + ".html"


def _write_snapshots(pages: list[tuple[str, str]], dest_dir: Path) -> None:
    """Write each recorded ``(url, html)`` to ``dest_dir`` as a hashed ``.html``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for url, html in pages:
        (dest_dir / _snapshot_name(url)).write_text(html, encoding="utf-8")


def _total_successes(registry: Registry, family: str) -> int:
    """The family's total successful-run count across its whole ladder.

    The snapshot cadence is measured against this running total (not a per-call
    counter), so it survives across :func:`run_family` invocations the way a
    real deployment would.
    """
    return sum(v["successes"] for v in registry.version_ladder(family))


async def run_family(
    family: str,
    url: str,
    ctx: FetchContext,
    *,
    registry: Registry,
    validate: Callable[[list[dict], str], ValidationLike],
    snapshots_dir: Path | None = None,
    snapshot_every: int = 20,
    max_pages: int = 10,
    now: str | None = None,
) -> FamilyRunResult:
    """Walk ``family``'s version ladder until one version's extraction validates.

    Versions are tried in :meth:`Registry.version_ladder` order — the active one
    first, then fallbacks newest-first. For each rung:

    1. Load it (``registry.load_crawler(family, n)`` — gated + integrity-checked).
    2. Wrap ``ctx`` in a FRESH :class:`RecordingFetchContext` so this attempt's
       fetched HTML is captured in isolation.
    3. Drive it via :func:`run_version` (pagination + the page cap), then
       ``validate(items, crawler.schema_ref)``.

    On the first validating rung: record a successful run + the extraction in
    history, snapshot the captured pages if ``snapshots_dir`` is set and the
    family's running successful-run total has reached the ``snapshot_every``
    cadence, and return a :class:`FamilyRunResult`. On a non-validating rung:
    record a failed run, remember its report, and fall through to the next.

    If no rung validates, raise :class:`AllVersionsFailed` carrying the LAST
    rung's report for the M8 classifier.

    Pagination, the page cap, and the central rate-limit/allowlist gate (via the
    injected context) are reused for every version, so a fallback is held to the
    exact same fetch discipline as the active one (design §5).
    """
    last_report: ValidationLike | None = None
    for entry in registry.version_ladder(family):
        n = entry["n"]
        crawler = registry.load_crawler(family, n)
        rec_ctx = RecordingFetchContext(ctx)
        items, pages = await run_version(crawler, url, rec_ctx, max_pages=max_pages)
        report = validate(items, crawler.schema_ref)

        if report.ok:
            registry.record_run(family, n, ok=True)
            registry.record_history(family, url, n, items, now=now)
            cadence_hit = _total_successes(registry, family) % snapshot_every == 0
            if snapshots_dir is not None and cadence_hit:
                _write_snapshots(rec_ctx.pages, snapshots_dir / family_dir(family))
            return FamilyRunResult(items=items, used_version=n, pages_fetched=pages)

        registry.record_run(family, n, ok=False)
        last_report = report

    reason = (
        last_report.reason
        if last_report is not None and last_report.reason
        else f"no version validated for family {family!r}"
    )
    raise AllVersionsFailed(family=family, reason=reason, last_report=last_report)
