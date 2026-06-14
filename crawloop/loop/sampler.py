"""The Loop's SAMPLE step (Task 9.1): collect a few fresh pages of a family.

Regeneration starts from real pages: the codegen step writes selectors against
sample HTML, and the oracle direct-extracts those same pages to define "correct".
One sample yields brittle, over-fit selectors; three or more force the model to
generalise across the family (design §9). :func:`collect_samples` gathers them.

It fetches the caller's ``seed_urls`` first (deduped, in order, each through the
injected :class:`~crawloop.contract.FetchContext` so the allowlist gate,
per-host rate limit, and access-recovery all apply). If that yields fewer than
``n`` pages it *harvests*: it parses the HTML it already has for ``<a href>``
links, absolutizes them, keeps only SAME-HOST links it has not already fetched —
same-host because the engine's allowlist would reject anything else and because
we specifically want more pages of the same family — and fetches those until it
reaches ``n`` or hits the ``max_fetch`` ceiling.

It is deliberately fault-tolerant: a seed or harvested URL that blocks, 404s, or
errors is skipped (its failure is swallowed), never fatal. Every fetch attempt —
success or failure — counts against ``max_fetch`` so an unreachable ``n`` (a page
whose links all 404) can never spin the loop forever; the sampler returns
whatever it managed to collect instead of hanging.
"""

from __future__ import annotations

from urllib.parse import urlparse

from parsel import Selector

# FetchBlocked/FetchError are the access layer's typed failures: a page being
# blocked or gone is normal to hit while harvesting and must skip that one URL,
# not abort sampling of the rest.
from crawloop.access import FetchBlocked, FetchError
from crawloop.contract import FetchContext


def _host(url: str) -> str:
    """Bare lowercase hostname of ``url`` (no port), the same key the access
    layer uses. Used to keep harvested links on the page's own host."""
    return (urlparse(url).hostname or "").lower()


def _harvest_links(html: str, page_url: str, ctx: FetchContext) -> list[str]:
    """Same-host, absolutized ``<a href>`` targets found in ``html``.

    Parses with parsel (the same library generated crawlers use), absolutizes
    each href against ``page_url`` via the context helper, and keeps only links
    whose host matches the page's host. Order is preserved and duplicates within
    the page are collapsed (first occurrence wins) so the caller's global de-dup
    sees a clean, ordered candidate stream.
    """
    page_host = _host(page_url)
    out: list[str] = []
    seen_here: set[str] = set()
    for href in Selector(text=html).css("a::attr(href)").getall():
        absolute = ctx.absolutize(page_url, href)
        if not absolute or absolute in seen_here:
            continue
        if _host(absolute) != page_host:
            continue  # off-host: the allowlist would reject it; not our family
        seen_here.add(absolute)
        out.append(absolute)
    return out


async def collect_samples(
    seed_urls: list[str],
    ctx: FetchContext,
    *,
    n: int = 3,
    max_fetch: int = 12,
) -> list[tuple[str, str]]:
    """Collect up to ``n`` ``(url, html)`` sample pages for a page family.

    Fetch ``seed_urls`` first (deduped, in order) through ``ctx``; then, if fewer
    than ``n`` pages were collected, harvest same-host links from the pages
    already fetched and fetch those until ``n`` is reached or ``max_fetch`` total
    fetch *attempts* have been made. URLs that block / 404 / error are skipped.

    Returns the collected pairs (at most ``n``). If even after harvesting fewer
    than ``n`` pages are reachable, returns what it has — it never hangs trying
    to reach an impossible ``n``.
    """
    collected: list[tuple[str, str]] = []
    seen: set[str] = set()  # every URL we have attempted (success OR failure)
    attempts = 0

    async def _try_fetch(url: str) -> None:
        """Fetch ``url`` once (counting the attempt), appending on success.

        Records the URL as seen regardless of outcome so we neither re-fetch nor
        re-harvest it, and swallows the access layer's typed failures so one bad
        page can't abort the run.
        """
        nonlocal attempts
        if url in seen:
            return
        seen.add(url)
        attempts += 1
        try:
            html = await ctx.fetch(url)
        except (FetchBlocked, FetchError):
            return
        collected.append((url, html))

    # 1) Seeds, in first-seen order, until we have n (or run out of seeds / the
    #    fetch budget). De-dup is handled by `seen` inside _try_fetch.
    for url in seed_urls:
        if len(collected) >= n or attempts >= max_fetch:
            break
        await _try_fetch(url)

    # 2) Harvest more same-host pages from what we have, breadth-first over the
    #    pages collected so far. Each newly fetched page is itself appended to
    #    `collected`, which we iterate by index, so the frontier expands
    #    naturally without a separate queue structure.
    i = 0
    while len(collected) < n and attempts < max_fetch and i < len(collected):
        page_url, page_html = collected[i]
        i += 1
        for link in _harvest_links(page_html, page_url, ctx):
            if len(collected) >= n or attempts >= max_fetch:
                break
            await _try_fetch(link)

    return collected[:n]
