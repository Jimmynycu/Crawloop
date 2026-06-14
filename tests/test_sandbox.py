"""Tests for the candidate SANDBOX (Task 9.3) — the security-critical layer that
executes LLM-generated crawler source.

The sandbox is the runtime half of the trust boundary: the AST gate
(:func:`crawloop.safety.ast_check`) statically vets source, and this runs the
vetted source in a *separate subprocess* with a wall-clock timeout and an OFFLINE
context (every ``ctx.fetch*`` just returns the one stored HTML — no sockets, no
network), so even a candidate that misbehaves at runtime is contained.

These tests assert the four security-relevant properties the M9 reviewer cares
about:

* the AST gate runs BEFORE any subprocess is spawned (a violating source raises
  ``ASTViolation`` and ``subprocess.run`` is never reached — proven by
  monkeypatching it to explode if called);
* a runaway candidate (infinite loop) is KILLED by the timeout and surfaces as
  ``SandboxTimeout`` (the test itself completes in ~the timeout, not forever);
* a candidate that raises inside ``crawl`` surfaces as ``SandboxError`` — the
  parent neither hangs nor crashes;
* a well-behaved crawler returns its extracted items.
"""

from __future__ import annotations

import pytest

from crawloop.loop import sandbox
from crawloop.loop.sandbox import (
    SandboxError,
    SandboxTimeout,
    run_in_sandbox,
)
from crawloop.safety import ASTViolation

# A valid books-listing crawler (design §5 shape): imports only from the allowed
# set, fetches via ctx.fetch (which the offline ctx satisfies with stored HTML),
# uses the ctx coercion helpers, returns a CrawlResult.
_VALID_CRAWLER = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksToscrapeProductList(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": str(ctx.parse_money(card.css(".price_color::text").get())),
                "in_stock": "In stock" in (ctx.clean_text(
                    " ".join(card.css(".availability::text").getall())) or ""),
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''

# A small known normal-layout listing (matches the fixture server's markup) so
# the happy-path test needs no live server.
_LISTING_HTML = (
    "<!DOCTYPE html><html><body><section><ol>"
    '<article class="product_pod"><h3>'
    '<a href="catalogue/a/index.html" title="A Light in the Attic">A</a></h3>'
    '<p class="price_color">£51.77</p>'
    '<p class="availability">In stock</p></article>'
    '<article class="product_pod"><h3>'
    '<a href="catalogue/b/index.html" title="Tipping the Velvet">B</a></h3>'
    '<p class="price_color">£53.74</p>'
    '<p class="availability">In stock</p></article>'
    '</ol><ul class="pager">'
    '<li class="next"><a href="catalogue/page-2.html">next</a></li>'
    "</ul></section></body></html>"
)


# --------------------------------------------------------------------------- #
# Happy path: a valid crawler extracts items from the stored HTML
# --------------------------------------------------------------------------- #


def test_run_in_sandbox_returns_items_for_valid_crawler():
    items = run_in_sandbox(_VALID_CRAWLER, _LISTING_HTML, url="https://books.local/p1")
    assert isinstance(items, list)
    assert len(items) == 2
    first = items[0]
    assert first["name"] == "A Light in the Attic"
    assert first["price"] == "51.77"
    assert first["in_stock"] is True
    # absolutize ran against the passed url
    assert first["url"] == "https://books.local/catalogue/a/index.html"


def test_run_in_sandbox_uses_stored_html_for_any_url():
    """The offline ctx ignores the URL and always serves the stored HTML — the
    crawler cannot reach the network. Two different URLs, same stored HTML, same
    item count; only absolutize (a pure string op) reflects the url."""
    items_a = run_in_sandbox(_VALID_CRAWLER, _LISTING_HTML, url="https://a.local/x")
    items_b = run_in_sandbox(_VALID_CRAWLER, _LISTING_HTML, url="https://b.local/y")
    assert len(items_a) == len(items_b) == 2
    assert items_a[0]["url"].startswith("https://a.local/")
    assert items_b[0]["url"].startswith("https://b.local/")


def test_run_in_sandbox_fetch_rendered_also_serves_stored_html():
    """A crawler that uses ctx.fetch_rendered gets the same offline HTML."""
    rendered_crawler = _VALID_CRAWLER.replace(
        "html = await ctx.fetch(url)",
        "html = await ctx.fetch_rendered(url, wait_for='.product_pod')",
    )
    items = run_in_sandbox(rendered_crawler, _LISTING_HTML, url="https://books.local/p1")
    assert len(items) == 2


# --------------------------------------------------------------------------- #
# SECURITY: the AST gate runs BEFORE any subprocess is spawned
# --------------------------------------------------------------------------- #


def test_ast_gate_runs_before_spawn_and_blocks_subprocess(monkeypatch):
    """An AST-violating source must raise ASTViolation and NEVER spawn a
    subprocess. We monkeypatch subprocess.run to blow up if it is ever called,
    so reaching it would fail the test with a different error."""

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("subprocess.run was called despite an AST violation")

    monkeypatch.setattr(sandbox.subprocess, "run", _boom)

    bad_source = "import os\nos.system('echo pwned')\n"
    with pytest.raises(ASTViolation):
        run_in_sandbox(bad_source, _LISTING_HTML)


def test_ast_violation_is_not_remapped_to_sandbox_error():
    """The gate failure surfaces as ASTViolation, not SandboxError — callers can
    distinguish 'never safe to run' from 'ran and failed'."""
    with pytest.raises(ASTViolation):
        run_in_sandbox("import socket\n", _LISTING_HTML)


# --------------------------------------------------------------------------- #
# SECURITY: a runaway candidate is killed by the timeout
# --------------------------------------------------------------------------- #


def test_infinite_loop_crawler_raises_sandbox_timeout():
    """A crawler that never returns is terminated by the wall-clock timeout and
    surfaces as SandboxTimeout. With timeout=2 the test completes in ~2s, proving
    the child is actually killed rather than the parent blocking forever."""
    infinite = '''\
from crawloop.contract import Crawler, CrawlResult, FetchContext


class Spin(Crawler):
    family = "x/y"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        while True:
            pass
'''
    with pytest.raises(SandboxTimeout):
        run_in_sandbox(infinite, _LISTING_HTML, timeout=2)


# --------------------------------------------------------------------------- #
# A crawler that raises inside crawl -> SandboxError (parent stays alive)
# --------------------------------------------------------------------------- #


def test_crawler_raising_inside_crawl_raises_sandbox_error():
    raises = '''\
from crawloop.contract import Crawler, CrawlResult, FetchContext


class Boom(Crawler):
    family = "x/y"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        raise RuntimeError("kaboom inside crawl")
'''
    with pytest.raises(SandboxError) as ei:
        run_in_sandbox(raises, _LISTING_HTML)
    # the child's error text is surfaced, not swallowed
    assert "kaboom" in str(ei.value)


def test_crawler_with_no_crawler_class_raises_sandbox_error():
    """Gate-passing source that defines no Crawler subclass is a runtime problem,
    not a security one: it must surface as SandboxError, not hang or crash."""
    no_class = "x = 1\ny = 2\n"
    with pytest.raises(SandboxError):
        run_in_sandbox(no_class, _LISTING_HTML)


# --------------------------------------------------------------------------- #
# SECURITY: the child does NOT inherit the parent's secrets (env scrub)
# --------------------------------------------------------------------------- #


def test_child_env_excludes_inherited_secrets(monkeypatch):
    """The env handed to the candidate subprocess must NOT carry the parent's
    secrets (API keys, WAF tokens, arbitrary creds). The AST gate already blocks
    os/env access, but the child should never even be *handed* them — defense in
    depth. `_child_env()` is the single env-builder, unit-tested here directly.

    A sentinel secret set on the parent must be absent from the built env, as
    must representative real ones; only the minimal interpreter needs (PATH) are
    carried.
    """
    monkeypatch.setenv("CL_SECRET_PROBE", "leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("EXAMPLE_WAF_TOKEN", "waf-should-not-leak")

    env = sandbox._child_env()

    assert "CL_SECRET_PROBE" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "EXAMPLE_WAF_TOKEN" not in env
    # PATH is carried so the interpreter can start.
    assert env.get("PATH") == __import__("os").environ.get("PATH", "")


def test_run_in_sandbox_passes_scrubbed_env_to_subprocess(monkeypatch):
    """End to end: the env actually passed to subprocess.run carries no inherited
    secret. Captured by recording the kwargs of a stubbed subprocess.run (which
    then raises so we don't need a real child for this assertion)."""
    monkeypatch.setenv("CL_SECRET_PROBE", "leak")
    captured: dict = {}

    def _record(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capturing kwargs")

    monkeypatch.setattr(sandbox.subprocess, "run", _record)

    with pytest.raises(RuntimeError):
        run_in_sandbox(_VALID_CRAWLER, _LISTING_HTML)

    assert "env" in captured  # an explicit env was passed (not the inherited one)
    assert "CL_SECRET_PROBE" not in captured["env"]


# --------------------------------------------------------------------------- #
# End-to-end against the real fixture server HTML
# --------------------------------------------------------------------------- #


async def test_run_in_sandbox_on_real_fixture_html(fixture_server):
    """Capture real fixture-server listing HTML, then run the valid crawler on it
    in the sandbox — the same markup a real generated crawler would face."""
    import httpx

    fixture_server.mode = "normal"
    async with httpx.AsyncClient(follow_redirects=False) as client:
        resp = await client.get(f"{fixture_server.url}/catalogue/page-1.html")
        html = resp.text

    items = run_in_sandbox(
        _VALID_CRAWLER, html, url=f"{fixture_server.url}/catalogue/page-1.html"
    )
    assert len(items) == 3  # page-1 has three books in the fixture
    assert all(it["name"] and it["price"] for it in items)
