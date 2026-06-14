"""Task 6.1 — the pagination driver (``run_version``).

Pagination lives in the executor, NOT in crawler code, so rate-limiting and the
page cap stay central (design §5). These tests run a small hand-written books
crawler against the *real* :class:`FixtureServer` through a *real*
:class:`RealFetchContext` (authorizing the fixture's ``127.0.0.1`` host), so the
whole fetch -> parse -> follow-next chain is genuinely exercised. Every httpx
client a test owns is closed via ``async with`` so none leak.
"""

from __future__ import annotations

import httpx
from parsel import Selector

from crawloop.access import RealFetchContext
from crawloop.config import AppConfig, DomainConfig
from crawloop.contract import CrawlResult, FetchContext
from crawloop.executor import run_version


# --------------------------------------------------------------------------- #
# Test doubles + a hand-written crawler parsing the NORMAL fixture layout
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    """In-memory BrowserRunner returning canned HTML (never used on the HTTP
    happy path, but RealFetchContext requires one)."""

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


class InMemoryAccessStore:
    """In-memory AccessStore (the shape M5's registry also satisfies)."""

    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


class BooksNormalCrawler:
    """A minimal Crawler parsing the fixture's NORMAL layout.

    Mirrors the generated-crawler shape from docs/design §5: one CSS pass over
    ``article.product_pod``, money coerced via the ctx, next-page link
    absolutized via the ctx. Pagination is the executor's job — this only
    reports its single page's items and the raw next link.
    """

    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            items.append(
                {
                    "name": card.css("h3 a::attr(title)").get(),
                    "price": ctx.parse_money(card.css(".price_color::text").get()),
                }
            )
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))


def _local_config(max_rps: float = 100.0) -> AppConfig:
    """Authorize the FixtureServer host (``127.0.0.1``) with a fast rate limit."""
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


# --------------------------------------------------------------------------- #
# run_version — accumulate across pages, follow next_url, honor max_pages
# --------------------------------------------------------------------------- #


async def test_run_version_paginates_across_two_pages(fixture_server):
    """page-1 (3 books) -> page-2 (1 book) -> no next: 4 items over 2 pages."""
    fixture_server.mode = "normal"
    start = f"{fixture_server.url}/catalogue/page-1.html"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        items, pages = await run_version(BooksNormalCrawler(), start, ctx)

    assert pages == 2
    assert len(items) == 4
    names = [it["name"] for it in items]
    assert names == [
        "A Light in the Attic",
        "Tipping the Velvet",
        "Soumission",
        "Sharp Objects",
    ]


async def test_run_version_stops_at_max_pages(fixture_server):
    """max_pages=1 fetches only page-1 and never follows the next link."""
    fixture_server.mode = "normal"
    start = f"{fixture_server.url}/catalogue/page-1.html"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        items, pages = await run_version(BooksNormalCrawler(), start, ctx, max_pages=1)

    assert pages == 1
    assert len(items) == 3  # only page-1's three books
    # The fixture server was hit exactly once (the page cap stopped the follow).
    assert fixture_server.hits == ["/catalogue/page-1.html"]


async def test_run_version_single_page_no_next(fixture_server):
    """A start page whose next_url is None returns just that page's items."""
    fixture_server.mode = "normal"
    start = f"{fixture_server.url}/catalogue/page-2.html"  # last page, no next
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        items, pages = await run_version(BooksNormalCrawler(), start, ctx)

    assert pages == 1
    assert len(items) == 1
    assert items[0]["name"] == "Sharp Objects"


async def test_run_version_returns_empty_when_layout_mismatch(fixture_server):
    """A crawler whose selectors find nothing yields zero items in one page (the
    self-referential/empty case the version-ladder walk relies on)."""
    fixture_server.mode = "mutated"  # normal-layout crawler finds nothing here
    start = f"{fixture_server.url}/catalogue/page-1.html"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _real_ctx(client)
        items, pages = await run_version(BooksNormalCrawler(), start, ctx)

    assert items == []
    assert pages == 1  # fetched the start page; found no next link -> stopped
