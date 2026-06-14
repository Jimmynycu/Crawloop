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


def test_live_real_world_full_loop_promotes_on_books_toscrape():
    """END-TO-END on a REAL public website over live HTTP (not a fixture).

    The loop learns a free deterministic crawler from real ``books.toscrape.com``
    detail pages (cheap model, auto-escalating to clear the gauntlet), promotes it,
    and that crawler then extracts a FRESH real page with ZERO model calls. This is
    the real-world proof; it needs network + a key, so it is gated like the rest.
    """
    import tempfile
    from pathlib import Path

    from crawloop.access import build_http_client
    from crawloop.config import load_config
    from crawloop.engine import Engine
    from crawloop.llm import LiteLLMCompleter
    from crawloop.loop.driver import run_loop
    from crawloop.registry import Registry

    class _NoBrowser:
        async def render(self, url, *, stealth, wait_for=None, extra_headers=None):
            raise RuntimeError("no browser needed for this test")

    B = "https://books.toscrape.com/catalogue/"

    async def _run() -> dict:
        cfg = load_config("authorized_domains.yaml")  # books.toscrape.com is allowlisted
        tmp = Path(tempfile.mkdtemp(prefix="rw-test-"))
        reg = Registry(db_path=str(tmp / "reg.db"), crawlers_dir=tmp / "crawlers")
        seeds = [B + "a-light-in-the-attic_1000/index.html",
                 B + "tipping-the-velvet_999/index.html",
                 B + "soumission_998/index.html"]
        async with build_http_client() as client:
            eng = Engine(cfg, reg, LiteLLMCompleter(), client=client,
                         browser_runner=_NoBrowser(), fixtures_dir=tmp / "fix", model=_MODEL)
            res = await run_loop(
                "books.toscrape.com/product_detail", seeds, eng._ctx, reg,
                LiteLLMCompleter(), "Product@1", fixtures_dir=tmp / "fix",
                model=_MODEL, n_samples=3, min_oracles=2, k=2, max_rounds=3,
            )
            assert res.ok, f"the loop did not promote on real pages: {res.reason}"
            src = reg.active_source("books.toscrape.com/product_detail")
            ns: dict = {}
            exec(src, ns)  # loop-generated + gauntlet-passed source
            cls = next(v for v in ns.values()
                       if isinstance(v, type) and getattr(v, "family", None))
            out = await cls().crawl(B + "sharp-objects_997/index.html", eng._ctx)
            assert len(out.items) == 1
            return out.items[0]

    rec = asyncio.run(_run())
    assert rec["name"]  # a real title was extracted, for free, on a fresh real page
    assert str(rec["url"]).startswith("https://books.toscrape.com/")
