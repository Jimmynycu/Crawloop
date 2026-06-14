"""Tests for the Loop's CODEGEN step (Task 9.2): :func:`generate_candidates`,
:func:`extract_code_block`, and the contract brief.

Codegen turns sample pages + their oracle JSON into candidate crawler *source*.
It builds one prompt (system + a ``string.Template`` user prompt carrying the
schema, a contract brief, the trimmed samples, the per-sample oracle JSON, the
previous version, and any failure report), calls the model ``k`` times for ``k``
independent candidates, extracts the fenced ```python``` block from each, and —
critically — runs every candidate through the AST gate, keeping only those that
pass. It does NOT execute candidates; that is the sandbox's job (Task 9.3).

All cases drive it with a :class:`FakeCompleter` (NO network, NO real model). The
gate is the real :func:`crawloop.safety.ast_check`, so "this candidate is
dropped" is proven against the actual trust boundary, not a stub.
"""

from __future__ import annotations

import pytest

from crawloop.llm import FakeCompleter
from crawloop.loop.codegen import (
    extract_code_block,
    generate_candidates,
)
from crawloop.safety import ast_check

# --------------------------------------------------------------------------- #
# Canned candidate sources
# --------------------------------------------------------------------------- #

# A valid books-listing crawler, mirroring design §5: imports only from the
# allowed set, fetches via ctx.fetch, uses the ctx coercion helpers, returns a
# CrawlResult. This must pass the AST gate untouched.
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
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in (ctx.clean_text(
                    " ".join(card.css(".availability::text").getall())) or ""),
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        next_href = sel.css("li.next a::attr(href)").get()
        return CrawlResult(items=items, next_url=ctx.absolutize(url, next_href))
'''

# Same shape but with a forbidden import. The AST gate must reject this, so
# generate_candidates must DROP it.
_IMPORT_OS_CRAWLER = '''\
import os
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class Evil(Crawler):
    family = "x/y"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        os.system("echo pwned")
        return CrawlResult(items=[], next_url=None)
'''


def _fenced(source: str, lang: str = "python") -> str:
    """Wrap source in a Markdown code fence as a model would emit it."""
    return f"Here is the crawler:\n\n```{lang}\n{source}```\n\nDone."


# Minimal sample + oracle inputs reused across tests.
_SAMPLES = [
    ("https://books.example.com/catalogue/page-1.html",
     '<article class="product_pod"><h3><a title="A">A</a></h3>'
     '<p class="price_color">£51.77</p></article>'),
]
_ORACLES = [
    [{"name": "A", "price": "51.77", "in_stock": True,
      "url": "https://books.example.com/a"}],
]


# --------------------------------------------------------------------------- #
# extract_code_block
# --------------------------------------------------------------------------- #


def test_extract_code_block_pulls_python_fence():
    text = _fenced("print('hi')\n")
    assert extract_code_block(text) == "print('hi')\n"


def test_extract_code_block_handles_bare_fence():
    text = "```\nprint('hi')\n```"
    assert extract_code_block(text) == "print('hi')\n"


def test_extract_code_block_no_fence_raises():
    with pytest.raises(ValueError):
        extract_code_block("no fence here, just prose")


# --------------------------------------------------------------------------- #
# generate_candidates — happy path
# --------------------------------------------------------------------------- #


async def test_generate_candidates_returns_gate_passing_source():
    fake = FakeCompleter([_fenced(_VALID_CRAWLER)])
    out = await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=1,
    )
    assert len(out) == 1
    # the returned source is exactly the fenced block's contents and is itself
    # gate-clean (the empty-violation contract the sandbox relies on).
    assert out[0].strip() == _VALID_CRAWLER.strip()
    assert ast_check(out[0]) == []


async def test_generate_candidates_calls_completer_k_times():
    # k=2 independent candidates -> two model calls, two kept sources.
    fake = FakeCompleter([_fenced(_VALID_CRAWLER), _fenced(_VALID_CRAWLER)])
    out = await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=2,
    )
    assert len(fake.calls) == 2
    assert len(out) == 2
    assert all(ast_check(src) == [] for src in out)


async def test_generate_candidates_passes_model_through():
    fake = FakeCompleter([_fenced(_VALID_CRAWLER)])
    await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake,
        model="anthropic/claude-test", k=1,
    )
    assert fake.calls[0]["model"] == "anthropic/claude-test"


# --------------------------------------------------------------------------- #
# generate_candidates — gate drops bad candidates
# --------------------------------------------------------------------------- #


async def test_generate_candidates_drops_gate_failing_candidate():
    # Two candidates: one valid, one with `import os`. Only the valid one
    # survives the AST gate.
    fake = FakeCompleter([_fenced(_VALID_CRAWLER), _fenced(_IMPORT_OS_CRAWLER)])
    out = await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=2,
    )
    assert len(out) == 1
    assert "import os" not in out[0]
    assert ast_check(out[0]) == []


async def test_generate_candidates_all_gate_failures_returns_empty():
    fake = FakeCompleter([_fenced(_IMPORT_OS_CRAWLER), _fenced(_IMPORT_OS_CRAWLER)])
    out = await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=2,
    )
    assert out == []


async def test_generate_candidates_drops_completion_without_code_fence():
    # A completion with no python block contributes no candidate (handled, not
    # fatal): the valid one still comes through.
    fake = FakeCompleter(["sorry, I cannot help with that", _fenced(_VALID_CRAWLER)])
    out = await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=2,
    )
    assert len(out) == 1
    assert ast_check(out[0]) == []


# --------------------------------------------------------------------------- #
# generate_candidates — the prompt actually carries schema + sample + oracle
# --------------------------------------------------------------------------- #


async def test_generate_candidates_prompt_contains_schema_sample_and_oracle():
    fake = FakeCompleter([_fenced(_VALID_CRAWLER)])
    await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=1,
    )
    call = fake.calls[0]
    assert call["system"].strip()  # non-empty system prompt
    user = call["user"]
    # schema JSON (a stable Product property name is a reliable marker)
    assert "in_stock" in user
    # a sample's HTML (the class attr survives trimming) and its URL
    assert "product_pod" in user
    assert "books.example.com/catalogue/page-1.html" in user
    # the oracle JSON for that sample (the oracle name/price appears)
    assert '"name": "A"' in user or '"name":"A"' in user
    assert "51.77" in user


async def test_generate_candidates_prompt_includes_prev_and_failure_when_given():
    prev = "class Old:\n    pass\n"
    failure = "Gate 2 failed: name fill-rate 0.40 < floor 0.95"
    fake = FakeCompleter([_fenced(_VALID_CRAWLER)])
    await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=prev, failure_report=failure, completer=fake, k=1,
    )
    user = fake.calls[0]["user"]
    assert "class Old" in user
    assert "fill-rate 0.40" in user


async def test_generate_candidates_prompt_says_none_when_no_prev_or_failure():
    fake = FakeCompleter([_fenced(_VALID_CRAWLER)])
    await generate_candidates(
        _SAMPLES, _ORACLES, "Product@1",
        prev_source=None, failure_report=None, completer=fake, k=1,
    )
    user = fake.calls[0]["user"]
    # the $previous / $failure placeholders are filled with an explicit "none"
    # sentinel rather than leaving a dangling template token.
    assert "$previous" not in user and "$failure" not in user
    assert "none" in user.lower()
