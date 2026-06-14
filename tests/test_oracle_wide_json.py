"""Capability #1: the oracle must read a WIDE embedded-JSON island in full.

The bootstrap oracle (``direct_extract``) was starved on 100K+ minified
``__NEXT_DATA__`` / ``ld+json`` islands: ``trim_html`` capped the model's whole
view at 8000 chars, so the gold record (deep in the island) was chopped off, the
oracle returned empty, fewer than ``min_oracles`` samples were usable, and the
regeneration loop could never promote on those pages. That is the project's
stated open blocker and the gap to Firecrawl/Crawl4AI on wide schemas.

These tests are fully offline. The model is an island-aware stub that returns the
gold record ONLY when the prompt actually contains the deep marker, so a pass
proves the SLICING (not model luck) was the blocker.
"""

from __future__ import annotations

import asyncio
import json

from crawloop.fallback import direct_extract
from crawloop.htmlutil import trim_html

# A marker that lives deep inside a large JSON island, well past the 8000 default
# cap, so it only survives when the JSON section is given a larger budget.
_GOLD_URL = "https://shop.example.com/deep-gold-marker"


def _wide_island_html() -> str:
    """An HTML page whose __NEXT_DATA__ island is ~30KB with the gold record LAST
    (so its marker sits far beyond char 8000), plus a long HTML body."""
    filler = {f"filler_{i}": "x" * 40 for i in range(600)}
    record = {**filler, "name": "DeepProduct", "price": "19.99",
              "in_stock": True, "url": _GOLD_URL}
    island = json.dumps({"props": {"pageProps": {"product": record}}})
    assert island.index("deep-gold-marker") > 8000  # gold is past the old cap
    body = "<p>" + ("filler body " * 4000) + "</p>"
    return (
        f"<html><body>{body}"
        f'<script id="__NEXT_DATA__" type="application/json">{island}</script>'
        f"</body></html>"
    )


def test_trim_html_keeps_wide_json_island_when_given_a_json_budget():
    out = trim_html(_wide_island_html(), json_max_chars=200_000)
    assert "[[STRUCTURED-DATA-JSON]]" in out
    assert "deep-gold-marker" in out  # the full island survived the trim


def test_trim_html_default_cap_still_truncates_the_island():
    # Documents the blocker (and guards the unchanged default): with no JSON
    # budget the deep gold is lost at 8000 chars.
    out = trim_html(_wide_island_html())
    assert len(out) <= 8000
    assert "deep-gold-marker" not in out


class _IslandAwareCompleter:
    """Returns the gold record ONLY if the prompt contains the deep marker — i.e.
    only if ``direct_extract`` handed the model the FULL island. Otherwise returns
    an empty array, exactly what a starved oracle produces."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        if "deep-gold-marker" in user:
            return json.dumps(
                [{"name": "DeepProduct", "price": "19.99",
                  "in_stock": True, "url": _GOLD_URL}]
            )
        return "[]"


def test_direct_extract_sees_full_island_and_returns_the_deep_record():
    completer = _IslandAwareCompleter()
    items = asyncio.run(direct_extract(_wide_island_html(), "Product@1", completer))
    assert len(items) == 1
    assert items[0]["url"] == _GOLD_URL
    # Prove it was the slicing: the prompt actually carried the deep marker.
    assert "deep-gold-marker" in completer.calls[0]["user"]


def test_direct_extract_passes_source_url_into_prompt():
    """The oracle / T2 must receive the page URL so the model can absolutize
    relative links — Product.url is a required HttpUrl, and a listing page's hrefs
    are usually relative. Offline we assert the URL reaches the prompt; the live
    smoke test proves the model then resolves relative links to absolute URLs."""
    from crawloop.llm import FakeCompleter

    html = (
        '<article class="product_pod"><h3><a href="catalogue/x/index.html" '
        'title="X">X</a></h3><p class="price_color">£5.00</p>'
        '<p class="availability">In stock</p></article>'
    )
    fake = FakeCompleter([json.dumps(
        [{"name": "X", "price": "5.00", "in_stock": True,
          "url": "http://h/catalogue/x/index.html"}]
    )])
    asyncio.run(direct_extract(html, "Product@1", fake,
                               source_url="http://h/catalogue/page-1.html"))
    assert "http://h/catalogue/page-1.html" in fake.calls[0]["user"]
