"""Integration tests (offline) for the deterministic-core + LLM-tail hybrid (Wave 3).

Proves the runtime half of the hybrid end-to-end through the REAL engine: the
:class:`FixtureServer` behind a real :class:`RealFetchContext`, a real in-memory
:class:`Registry` / executor / validator, with the ONLY fake being the
:class:`FakeCompleter`. No real model, no network beyond localhost.

The setup mirrors ``tests/test_engine.py``'s fast-path test but registers a
deterministic crawler that intentionally OMITS one field (``image_url`` — optional on
``Product@1``, so the record still validates and the registry fast path serves it),
and persists ``residual_fields=["image_url"]`` for the active version (the same
promote-audit metadata the driver writes). Then:

* **hybrid ON** — the engine serves the deterministic core (free) and makes EXACTLY
  ONE small LLM call to fill ``image_url``, merging it into every item. The result
  is the COMPLETE record (deterministic core + tail-filled field) with
  ``hybrid_filled`` True and a ``hybrid_fill`` audit entry.
* **hybrid OFF** / **offline** — the engine returns the deterministic-only record
  (no ``image_url``) with ZERO completer calls — the cost guarantee.
* **empty residual set** — even with the hybrid on, a family whose crawler is
  complete makes ZERO completer calls ($0).
"""

from __future__ import annotations

import httpx

from crawloop.access import build_http_client
from crawloop.config import AppConfig, DomainConfig
from crawloop.engine import Engine
from crawloop.llm import FakeCompleter
from crawloop.registry import Registry

SCHEMA = "Product@1"
FAMILY = "127.0.0.1/product_list"
LISTING_PATTERN = r"^https?://127\.0\.0\.1.*/catalogue/.*"

# A deterministic crawler for the NORMAL layout that DELIBERATELY omits image_url
# (optional on Product@1, so the record validates and the registry fast path serves
# it — image_url is exactly the kind of field a deterministic crawler leaves blank).
# No pagination, so the requested page yields its own 3 books.
_OMITS_IMAGE_URL = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksNoImage(Crawler):
    family = "127.0.0.1/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            avail = ctx.clean_text(" ".join(card.css(".availability::text").getall())) or ""
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in avail,
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        return CrawlResult(items=items, next_url=None)
'''


class FakeBrowserRunner:
    async def render(self, url, *, stealth, wait_for=None, extra_headers=None) -> str:
        return "<html>rendered</html>"


def _local_config() -> AppConfig:
    dc = DomainConfig(
        domain="127.0.0.1",
        max_rps=1000.0,
        render_js=False,
        access_strategies=[("plain", {})],
    )
    return AppConfig(respect_robots=False, domains={"127.0.0.1": dc})


def _seed_family_with_residual(
    registry: Registry, residual_fields: list[str]
) -> None:
    """Register the image-omitting crawler as the active version + persist its residual set.

    Writes a ``promote`` audit carrying ``residual_fields`` exactly as the driver's
    promote step does, so :meth:`Registry.active_residual_fields` reads it back.
    """
    registry.upsert_family(FAMILY, [LISTING_PATTERN], SCHEMA)
    n = registry.add_version(FAMILY, _OMITS_IMAGE_URL)  # v1
    registry.set_active(FAMILY, n)
    registry.write_audit(
        "promote",
        family=FAMILY,
        data={"to_version": n, "residual_fields": residual_fields},
    )


def _engine(
    config: AppConfig,
    registry: Registry,
    completer: FakeCompleter,
    client: httpx.AsyncClient,
    tmp_path,
    *,
    hybrid: bool = True,
    offline: bool = False,
) -> Engine:
    return Engine(
        config,
        registry,
        completer,
        client=client,
        browser_runner=FakeBrowserRunner(),
        fixtures_dir=tmp_path / "fixtures",
        run_loop_inline=False,
        hybrid=hybrid,
        offline=offline,
        now="2026-06-13T00:00:00+00:00",
    )


_IMG = "https://img.example.com/a-light.jpg"


async def test_hybrid_fills_residual_field_with_one_llm_call(fixture_server, tmp_path):
    """Hybrid ON: deterministic core served + ONE LLM call fills image_url, merged in."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_family_with_residual(registry, ["image_url"])
    # The single residual call returns the image_url; one canned response is enough.
    completer = FakeCompleter([f'{{"image_url": "{_IMG}"}}'])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    # Served by the deterministic ladder (the hybrid enriches, it does not replace).
    assert result.source == "registry"
    assert result.used_version == 1
    assert result.reason == "ok"
    assert result.hybrid_filled is True

    # Every item now carries the tail-filled image_url AND its deterministic core.
    assert len(result.items) == 3
    assert {it["name"] for it in result.items} == {
        "A Light in the Attic", "Tipping the Velvet", "Soumission",
    }
    assert all(str(it["image_url"]) == _IMG for it in result.items)
    # Deterministic core is intact (price/in_stock/url present from the crawler).
    a_light = next(it for it in result.items if it["name"] == "A Light in the Attic")
    assert str(a_light["price"]) == "51.77"
    assert a_light["in_stock"] is True

    # THE CRUX: exactly ONE small LLM call for the residual field — not per item,
    # not per page-field, just one targeted fill.
    assert len(completer.calls) == 1

    # A cheap hybrid_fill audit signal was recorded.
    fills = [e for e in registry.read_audit(FAMILY) if e["event"] == "hybrid_fill"]
    assert len(fills) == 1
    assert fills[0]["data"]["residual_fields"] == ["image_url"]
    assert fills[0]["data"]["filled"] == ["image_url"]


async def test_hybrid_disabled_returns_deterministic_only_zero_calls(
    fixture_server, tmp_path
):
    """Hybrid OFF: the deterministic-only record is served with ZERO completer calls."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_family_with_residual(registry, ["image_url"])
    completer = FakeCompleter([])  # would raise if called

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path, hybrid=False
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "registry"
    assert result.hybrid_filled is False
    assert len(result.items) == 3
    # The residual field stays blank (deterministic-only) — no key or None.
    assert all(it.get("image_url") is None for it in result.items)
    # THE CRUX of "disabled": no model call at all.
    assert completer.calls == []
    assert [e for e in registry.read_audit(FAMILY) if e["event"] == "hybrid_fill"] == []


async def test_hybrid_offline_returns_deterministic_only_zero_calls(
    fixture_server, tmp_path
):
    """Offline: same deterministic-only serve, ZERO completer calls (no model offline)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_family_with_residual(registry, ["image_url"])
    completer = FakeCompleter([])  # the offline completer would raise if called

    async with build_http_client() as client:
        engine = _engine(
            _local_config(), registry, completer, client, tmp_path, offline=True
        )
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "registry"
    assert result.hybrid_filled is False
    assert all(it.get("image_url") is None for it in result.items)
    assert completer.calls == []


async def test_hybrid_empty_residual_set_makes_zero_calls(fixture_server, tmp_path):
    """A complete crawler (empty residual set) makes ZERO calls even with hybrid on ($0)."""
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_family_with_residual(registry, [])  # no residual fields persisted
    completer = FakeCompleter([])  # would raise if called

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "registry"
    assert result.hybrid_filled is False
    # THE CRUX of the cost guarantee: empty residual set -> no LLM call.
    assert completer.calls == []


async def test_hybrid_bad_residual_response_keeps_deterministic_record(
    fixture_server, tmp_path
):
    """A malformed residual response is swallowed: deterministic items stand, no fill flag.

    The one call still happens (the residual set is non-empty), but its garbage output
    yields {} from fill_residual, so the items are unchanged and hybrid_filled is False
    — the deterministic record always stands even when the tail-fill misfires.
    """
    fixture_server.mode = "normal"
    base = fixture_server.url
    registry = Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")
    _seed_family_with_residual(registry, ["image_url"])
    completer = FakeCompleter(["not json at all"])

    async with build_http_client() as client:
        engine = _engine(_local_config(), registry, completer, client, tmp_path)
        result = await engine.request(f"{base}/catalogue/page-1.html")

    assert result.source == "registry"
    assert result.hybrid_filled is False
    assert all(it.get("image_url") is None for it in result.items)
    # The call was made (residual set non-empty) but produced nothing usable.
    assert len(completer.calls) == 1
    assert [e for e in registry.read_audit(FAMILY) if e["event"] == "hybrid_fill"] == []
