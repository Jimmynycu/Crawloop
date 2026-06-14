"""Optional LIVE smoke tests — the real-model datapoints that prove the loop is
not merely an offline simulation.

SKIPPED by default (keeps the hermetic suite offline). Run explicitly:

    RUN_LIVE_LLM=1 .venv/bin/python -m pytest tests/test_live_llm_smoke.py -q

Uses ``OPENAI_API_KEY`` (loaded from a local ``.env`` if not already exported) and
a cheap model (``LIVE_MODEL`` env, default ``openai/gpt-4o-mini``). Costs a fraction
of a cent per test. Only the MODEL call is live — the HTML is built inline, so there
is no network fetch and the comparison is against a known gold record.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest

_RUN = os.getenv("RUN_LIVE_LLM")


def _load_openai_key() -> bool:
    if os.getenv("OPENAI_API_KEY"):
        return True
    env = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                return True
    return False


_HAS_KEY = _load_openai_key() if _RUN else False
_MODEL = os.getenv("LIVE_MODEL", "openai/gpt-4o-mini")

pytestmark = pytest.mark.skipif(
    not (_RUN and _HAS_KEY),
    reason="live LLM test: set RUN_LIVE_LLM=1 and provide OPENAI_API_KEY (.env is read automatically)",
)


def test_live_direct_extract_narrow_product():
    """The oracle / T2 path returns a schema-valid record from a real model."""
    from crawloop.fallback import direct_extract
    from crawloop.llm import LiteLLMCompleter

    html = (
        '<article class="product_pod"><h3><a '
        'href="https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html" '
        'title="A Light in the Attic">A Light in the Attic</a></h3>'
        '<p class="price_color">£51.77</p>'
        '<p class="instock availability">In stock</p></article>'
    )
    items = asyncio.run(direct_extract(html, "Product@1", LiteLLMCompleter(), model=_MODEL))
    assert len(items) >= 1
    assert items[0]["name"] == "A Light in the Attic"
    assert float(items[0]["price"]) == 51.77
    assert items[0]["in_stock"] is True


def test_live_direct_extract_wide_json_island_deep_record():
    """Capability #1 proven end-to-end with a REAL model: a record buried in a
    ~50KB ``__NEXT_DATA__`` island (well past the old 8000-char cap) is extracted —
    the gap to Firecrawl/Crawl4AI on wide schemas, demonstrated live."""
    from crawloop.fallback import direct_extract
    from crawloop.llm import LiteLLMCompleter

    filler = {f"f_{i}": "x" * 40 for i in range(1000)}
    prod = {**filler, "name": "Deep Catalogue Item", "price": "73.99",
            "in_stock": True, "url": "https://books.toscrape.com/catalogue/deep_999/index.html"}
    island = json.dumps({"props": {"pageProps": {"product": prod}}})
    assert island.index("deep_999") > 40_000  # gold is far past the old cap
    html = (
        f'<html><body><p>{"nav " * 2000}</p>'
        f'<script id="__NEXT_DATA__" type="application/json">{island}</script></body></html>'
    )
    items = asyncio.run(direct_extract(html, "Product@1", LiteLLMCompleter(), model=_MODEL))
    assert len(items) >= 1
    assert any("deep_999" in (it.get("url") or "") for it in items)
