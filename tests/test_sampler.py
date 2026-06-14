"""Tests for the Loop's sampler (Task 9.1): :func:`collect_samples`.

The sampler is the SAMPLE step of the regeneration loop (design §9): given a few
seed URLs for a page family it fetches fresh HTML through the access layer, and
— if it has fewer than ``n`` pages — harvests more same-host links out of the
HTML it already has and fetches those, so the codegen step always sees several
real pages (one sample -> brittle selectors; three+ forces generalization).

Everything runs against the real :class:`FixtureServer` (so the HTTP path and
parsel link-harvesting are genuinely exercised) behind a :class:`RealFetchContext`
that authorizes only ``127.0.0.1``. A fake :class:`BrowserRunner` and in-memory
:class:`AccessStore` stand in for the parts a sampler never needs. No network
beyond localhost; every httpx client is closed via ``async with``.

Fixture-server reachability (single source of truth in the server): exactly three
routes return 200 — ``/``, ``/catalogue/page-1.html``, ``/catalogue/page-2.html``
— and every product *detail* href (e.g. ``catalogue/a-light-in-the-attic/...``)
404s. So harvesting from a listing page surfaces the same-host ``next`` link
(reachable) alongside detail links (404); the sampler must keep the reachable
pages and quietly skip the 404s without hanging.
"""

from __future__ import annotations

import httpx
from parsel import Selector

from crawloop.access import RealFetchContext
from crawloop.config import AppConfig, DomainConfig
from crawloop.loop.sampler import collect_samples


# --------------------------------------------------------------------------- #
# Test doubles (mirror tests/test_fetch_context.py)
# --------------------------------------------------------------------------- #


class FakeBrowserRunner:
    """In-memory :class:`BrowserRunner` returning canned HTML (never launched)."""

    def __init__(self, html: str = "<html>rendered</html>"):
        self._html = html
        self.calls: list[dict] = []

    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        self.calls.append({"url": url, "stealth": stealth, "wait_for": wait_for})
        return self._html


class InMemoryAccessStore:
    """In-memory :class:`AccessStore` (the shape M5's registry satisfies)."""

    def __init__(self):
        self.working: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def get_working_strategy(self, domain: str) -> str | None:
        return self.working.get(domain)

    def set_working_strategy(self, domain: str, strategy: str) -> None:
        self.working[domain] = strategy

    def mark_domain_status(self, domain: str, status: str) -> None:
        self.statuses[domain] = status


def _local_config(*, max_rps: float = 100.0) -> AppConfig:
    """Authorize the FixtureServer host (``127.0.0.1``) with a plain HTTP policy."""
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=max_rps,
        render_js=False,
        access_strategies=[("plain", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _ctx(client: httpx.AsyncClient) -> RealFetchContext:
    return RealFetchContext(
        _local_config(),
        InMemoryAccessStore(),
        client=client,
        browser_runner=FakeBrowserRunner(),
    )


# --------------------------------------------------------------------------- #
# Harvesting: a single listing seed grows to >= n via same-host links
# --------------------------------------------------------------------------- #


async def test_collect_samples_harvests_to_n_distinct_same_host_pairs(fixture_server):
    """Two listing seeds + harvested ``next`` link -> >= n distinct same-host
    pairs, all non-empty HTML. Harvesting follows ``a::attr(href)`` from the
    already-fetched pages and keeps only reachable same-host links (the detail
    links 404 and are silently skipped)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    seeds = [f"{base}/", f"{base}/catalogue/page-1.html"]

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        samples = await collect_samples(seeds, ctx, n=3)

    assert len(samples) >= 3
    urls = [u for u, _ in samples]
    # distinct URLs, all same host, all non-empty HTML
    assert len(set(urls)) == len(urls)
    assert all(u.startswith(base) for u in urls)
    assert all(html.strip() for _, html in samples)
    # the harvested page-2 (the "next" link) made it in — proof harvesting ran
    assert any(u.endswith("/catalogue/page-2.html") for u in urls)


async def test_collect_samples_single_seed_harvests_next_link(fixture_server):
    """A single listing seed still harvests the reachable same-host ``next``
    link (page-2). The three detail links 404; the sampler skips them without
    hanging and returns the pages it could actually fetch."""
    fixture_server.mode = "normal"
    base = fixture_server.url

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        samples = await collect_samples([f"{base}/catalogue/page-1.html"], ctx, n=3)

    urls = [u for u, _ in samples]
    assert f"{base}/catalogue/page-1.html" in urls
    assert f"{base}/catalogue/page-2.html" in urls  # harvested next link
    # Only two pages are reachable in the fixture; sampler returns what it has
    # rather than hanging trying to reach an impossible n.
    assert len(samples) == 2
    # what it did fetch is real listing HTML
    for _, html in samples:
        assert Selector(text=html).css("article.product_pod")


# --------------------------------------------------------------------------- #
# Enough seeds: no harvesting, no over-fetch
# --------------------------------------------------------------------------- #


async def test_collect_samples_enough_seeds_does_not_overfetch(fixture_server):
    """When the seeds already satisfy ``n``, the sampler fetches exactly the
    seeds (deduped) and harvests nothing — it must not crawl extra pages."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    seeds = [
        f"{base}/",
        f"{base}/catalogue/page-1.html",
        f"{base}/catalogue/page-2.html",
    ]
    fixture_server.hits.clear()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        samples = await collect_samples(seeds, ctx, n=3)

    assert len(samples) == 3
    assert [u for u, _ in samples] == seeds  # order preserved
    # Exactly the three seed GETs hit the server — no harvested extras.
    assert fixture_server.hits == [
        "/",
        "/catalogue/page-1.html",
        "/catalogue/page-2.html",
    ]


async def test_collect_samples_dedups_seed_urls(fixture_server):
    """Duplicate seeds are fetched once, in first-seen order."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    seeds = [
        f"{base}/catalogue/page-1.html",
        f"{base}/catalogue/page-1.html",  # dup
        f"{base}/catalogue/page-2.html",
    ]
    fixture_server.hits.clear()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        samples = await collect_samples(seeds, ctx, n=2)

    # n=2 satisfied by the two distinct seeds; the dup is not re-fetched.
    assert [u for u, _ in samples] == [
        f"{base}/catalogue/page-1.html",
        f"{base}/catalogue/page-2.html",
    ]
    assert fixture_server.hits == ["/catalogue/page-1.html", "/catalogue/page-2.html"]


async def test_collect_samples_caps_total_fetches_at_max_fetch(fixture_server):
    """``max_fetch`` is a hard ceiling on total fetch attempts even if ``n`` is
    never reached — the sampler must not loop forever harvesting."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    fixture_server.hits.clear()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        # n is unreachable (only 3 routes 200) but max_fetch bounds the attempts.
        samples = await collect_samples(
            [f"{base}/catalogue/page-1.html"], ctx, n=99, max_fetch=4
        )

    assert len(fixture_server.hits) <= 4
    # whatever it returned is within n and all same-host non-empty
    assert all(u.startswith(base) and html.strip() for u, html in samples)


async def test_collect_samples_empty_seeds_returns_empty(fixture_server):
    """No seeds -> nothing to fetch or harvest; returns [] without error."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)
        assert await collect_samples([], ctx, n=3) == []


async def test_collect_samples_skips_offhost_links(fixture_server, monkeypatch):
    """Harvested links pointing at a different host are dropped before any
    fetch (the engine's allowlist would reject them anyway, and we want
    same-family pages). We prove it by injecting an off-host anchor into the
    fetched HTML and asserting the sampler never tries to fetch it."""
    fixture_server.mode = "normal"
    base = fixture_server.url

    async with httpx.AsyncClient(follow_redirects=False) as client:
        ctx = _ctx(client)

        real_fetch = ctx.fetch
        fetched: list[str] = []

        async def recording_fetch(url: str) -> str:
            fetched.append(url)
            html = await real_fetch(url)
            # Splice an off-host link into the listing so harvesting sees it.
            return html.replace(
                "</body>", '<a href="https://evil.example.com/x">off</a></body>'
            )

        monkeypatch.setattr(ctx, "fetch", recording_fetch)
        await collect_samples([f"{base}/catalogue/page-1.html"], ctx, n=3)

    assert not any("evil.example.com" in u for u in fetched)
