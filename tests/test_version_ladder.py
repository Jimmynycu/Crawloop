"""Task 6.2 — the runtime version-ladder walk (``run_family``) + snapshotting.

The KEY behavior under test is *fallthrough*: ``run_family`` tries the family's
active version first, and if that version's extraction does not validate it falls
to the next rung (newest fallback first) until one validates — recording a run
result on every attempt. These tests register two real, AST-gated crawler
versions (one parsing the fixture's NORMAL layout, one the MUTATED layout) into a
real :class:`Registry`, then run against the real :class:`FixtureServer` through a
real :class:`RealFetchContext`. Validation is an injected fake (ok iff >=1 item),
honoring the executor's dependency inversion on the not-yet-built M7 validator.

Every httpx client a test owns is closed via ``async with`` so none leak.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from crawloop.access import RealFetchContext
from crawloop.config import AppConfig, DomainConfig
from crawloop.executor import (
    AllVersionsFailed,
    FamilyRunResult,
    RecordingFetchContext,
    run_family,
)
from crawloop.registry import Registry, family_dir

FAMILY = "books.toscrape.com/product_list"


# --------------------------------------------------------------------------- #
# Two real, AST-clean crawler sources: one per fixture layout
# --------------------------------------------------------------------------- #

# v1 parses the NORMAL layout (article.product_pod / .price_color / li.next).
NORMAL_SOURCE = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksNormal(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
            })
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''

# v2 parses the MUTATED layout (div.card / .price-box / div.pager-next).
MUTATED_SOURCE = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksMutated(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("div.card"):
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price-box::text").get()),
            })
        next_href = sel.css("div.pager-next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''


# --------------------------------------------------------------------------- #
# Test doubles + builders
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


class InMemoryAccessStore:
    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


class _Report:
    """A minimal ValidationLike: ``ok`` + ``reason``."""

    def __init__(self, ok: bool, reason: str = ""):
        self.ok = ok
        self.reason = reason


def fake_validate(items, schema_ref):
    """ok iff at least one item was extracted; else reason='empty'."""
    if len(items) >= 1:
        return _Report(True, "")
    return _Report(False, "empty")


def always_fail_validate(items, schema_ref):
    """Never validates (used for the all-fail path)."""
    return _Report(False, "forced failure")


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


def _local_config(max_rps: float = 100.0) -> AppConfig:
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=max_rps,
        render_js=False,
        access_strategies=[("plain", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _real_ctx(client: httpx.AsyncClient) -> RealFetchContext:
    return RealFetchContext(
        _local_config(),
        InMemoryAccessStore(),
        client=client,
        browser_runner=FakeBrowserRunner(),
    )


def _register_v1_normal_v2_mutated(registry: Registry) -> None:
    """v1 = NORMAL-layout crawler, v2 = MUTATED-layout crawler, v2 active."""
    registry.add_version(FAMILY, NORMAL_SOURCE)  # v1
    registry.add_version(FAMILY, MUTATED_SOURCE)  # v2
    registry.set_active(FAMILY, 2)


def _runs_for(registry: Registry, n: int) -> tuple[int, int]:
    """(runs, successes) for version ``n`` from the ladder."""
    (entry,) = [v for v in registry.version_ladder(FAMILY) if v["n"] == n]
    return entry["runs"], entry["successes"]


# --------------------------------------------------------------------------- #
# RecordingFetchContext — decorator that captures (url, html), delegates the rest
# --------------------------------------------------------------------------- #


async def test_recording_context_captures_fetch_and_delegates(fixture_server):
    fixture_server.mode = "normal"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        inner = _real_ctx(client)
        rec = RecordingFetchContext(inner)
        url = f"{fixture_server.url}/catalogue/page-1.html"
        html = await rec.fetch(url)

    # The (url, html) pair was captured...
    assert rec.pages == [(url, html)]
    assert "product_pod" in html
    # ...and the coercion helpers delegate to the wrapped context.
    assert rec.absolutize("http://x/a/b", "../c") == "http://x/c"
    assert rec.parse_money("£51.77") == Decimal("51.77")
    assert rec.clean_text("  a  b ") == "a b"


async def test_recording_context_records_rendered(fixture_server):
    fixture_server.mode = "normal"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        inner = _real_ctx(client)
        rec = RecordingFetchContext(inner)
        url = f"{fixture_server.url}/catalogue/page-1.html"
        html = await rec.fetch_rendered(url)  # FakeBrowserRunner returns canned html
    assert rec.pages == [(url, html)]


# --------------------------------------------------------------------------- #
# run_family — the version-ladder walk (the KEY fallthrough behavior)
# --------------------------------------------------------------------------- #


async def test_run_family_falls_through_active_to_working_fallback(registry, fixture_server):
    """Active v2 (mutated layout) finds 0 items on the NORMAL server -> invalid;
    walk falls to v1 (normal layout) which validates. used_version == 1, and a
    failure is recorded for v2 + a success for v1."""
    fixture_server.mode = "normal"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        result = await run_family(
            FAMILY, url, ctx, registry=registry, validate=fake_validate
        )

    assert isinstance(result, FamilyRunResult)
    assert result.used_version == 1
    assert result.pages_fetched == 2  # v1 paginates page-1 -> page-2
    assert len(result.items) == 4
    # record_run was called on EACH attempt: v2 a failure, v1 a success.
    assert _runs_for(registry, 2) == (1, 0)
    assert _runs_for(registry, 1) == (1, 1)


async def test_run_family_uses_active_when_it_validates(registry, fixture_server):
    """Same registry, server MUTATED: active v2 validates -> used_version == 2,
    and v1 is never tried (no run recorded for it)."""
    fixture_server.mode = "mutated"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        result = await run_family(
            FAMILY, url, ctx, registry=registry, validate=fake_validate
        )

    assert result.used_version == 2
    assert len(result.items) == 4
    assert _runs_for(registry, 2) == (1, 1)
    # The active version validated first, so the fallback was never run.
    assert _runs_for(registry, 1) == (0, 0)


async def test_run_family_all_versions_fail_raises(registry, fixture_server):
    """A registry whose only version cannot extract on the current mode raises
    AllVersionsFailed carrying the last failing report."""
    fixture_server.mode = "mutated"  # the only registered version parses NORMAL
    registry.add_version(FAMILY, NORMAL_SOURCE)
    registry.set_active(FAMILY, 1)
    url = f"{fixture_server.url}/catalogue/page-1.html"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        with pytest.raises(AllVersionsFailed) as ei:
            await run_family(FAMILY, url, ctx, registry=registry, validate=fake_validate)

    err = ei.value
    assert err.family == FAMILY
    assert err.last_report is not None
    assert err.last_report.ok is False
    assert err.reason  # non-empty reason for the M8 classifier
    # The single version's failed attempt was still recorded.
    assert _runs_for(registry, 1) == (1, 0)


async def test_run_family_all_versions_fail_even_when_validate_always_fails(
    registry, fixture_server
):
    """Even with a version that DOES extract, a validate that always rejects must
    exhaust the ladder and raise (proves validation, not just emptiness, gates)."""
    fixture_server.mode = "normal"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        with pytest.raises(AllVersionsFailed):
            await run_family(
                FAMILY, url, ctx, registry=registry, validate=always_fail_validate
            )
    # Both rungs were attempted and recorded as failures.
    assert _runs_for(registry, 2) == (1, 0)
    assert _runs_for(registry, 1) == (1, 0)


# --------------------------------------------------------------------------- #
# Snapshotting — cadence-gated HTML capture under snapshots_dir/<family_dir>/
# --------------------------------------------------------------------------- #


async def test_run_family_writes_snapshot_on_cadence(registry, fixture_server, tmp_path):
    """snapshot_every=1: after the first successful run, each fetched page's HTML
    is written under snapshots_dir/<family_dir(family)>/ as a hashed .html file
    containing the listing markup."""
    fixture_server.mode = "mutated"
    _register_v1_normal_v2_mutated(registry)  # active v2 matches mutated
    url = f"{fixture_server.url}/catalogue/page-1.html"
    snaps = tmp_path / "snapshots"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        await run_family(
            FAMILY, url, ctx,
            registry=registry, validate=fake_validate,
            snapshots_dir=snaps, snapshot_every=1,
        )

    fam_dir = snaps / family_dir(FAMILY)
    written = list(fam_dir.glob("*.html"))
    assert written, "expected at least one snapshot file"
    # v2 paginates two pages, so both fetched pages were snapshotted.
    assert len(written) == 2
    combined = "".join(p.read_text(encoding="utf-8") for p in written)
    assert "card" in combined  # the mutated listing markup was captured


async def test_run_family_no_snapshot_below_cadence(registry, fixture_server, tmp_path):
    """snapshot_every=20 with a single successful run (success count == 1) does
    NOT hit the cadence, so nothing is written."""
    fixture_server.mode = "mutated"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"
    snaps = tmp_path / "snapshots"

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        await run_family(
            FAMILY, url, ctx,
            registry=registry, validate=fake_validate,
            snapshots_dir=snaps, snapshot_every=20,
        )
    # No snapshot files (cadence not reached).
    fam_dir = snaps / family_dir(FAMILY)
    assert not fam_dir.exists() or not list(fam_dir.glob("*.html"))


async def test_run_family_no_snapshot_when_dir_none(registry, fixture_server):
    """With snapshots_dir=None the run still succeeds and writes no snapshot."""
    fixture_server.mode = "mutated"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        result = await run_family(
            FAMILY, url, ctx, registry=registry, validate=fake_validate,
            snapshots_dir=None, snapshot_every=1,
        )
    assert result.used_version == 2


# --------------------------------------------------------------------------- #
# run_history — a successful run records its extracted items
# --------------------------------------------------------------------------- #


async def test_run_family_records_history_on_success(registry, fixture_server):
    fixture_server.mode = "mutated"
    _register_v1_normal_v2_mutated(registry)
    url = f"{fixture_server.url}/catalogue/page-1.html"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        result = await run_family(
            FAMILY, url, ctx, registry=registry, validate=fake_validate
        )

    history = registry.recent_history(FAMILY, url)
    assert len(history) == 1
    row = history[0]
    assert row["version"] == result.used_version
    # The extracted items were persisted.
    names = [it["name"] for it in row["items"]]
    assert "A Light in the Attic" in names
    assert len(row["items"]) == len(result.items)
