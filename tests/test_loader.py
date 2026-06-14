"""Task 5.2 — the gated crawler loader.

``Registry.load_crawler`` turns a registered version's on-disk file into a live
:class:`~crawloop.contract.Crawler` instance. The security-critical property
under test: the AST gate runs AGAIN at load time, so a file tampered with after
registration (when ``add_version`` gated it) is still rejected. Tests use a real
on-disk ``crawlers_dir`` and write/append real files.
"""

from __future__ import annotations

import asyncio

import pytest

from crawloop.contract import Crawler
from crawloop.registry import IntegrityError, Registry, family_dir
from crawloop.safety import ASTViolation

# Valid generated crawler (only allowlisted imports / no banned constructs).
BOOKS_SOURCE = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksToscrapeProductList(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(html)
        items = [{"name": c.css("h3 a::attr(title)").get()}
                 for c in sel.css("article.product_pod")]
        return CrawlResult(items=items)
'''

FAMILY = "books.toscrape.com/product_list"


class FakeCtx:
    """Minimal FetchContext stand-in so a loaded crawler can actually run."""

    async def fetch(self, url):
        return '<article class="product_pod"><h3><a title="Dune"></a></h3></article>'

    async def fetch_rendered(self, url, wait_for=None):
        return await self.fetch(url)

    def absolutize(self, base, href):
        return href

    def parse_money(self, raw):
        return None

    def clean_text(self, raw):
        return raw


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


def test_load_active_crawler_returns_usable_instance(registry):
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    crawler = registry.load_crawler(FAMILY)  # no n -> resolve the active version
    # It is a real Crawler whose declared identity matches the source.
    assert isinstance(crawler, Crawler)
    assert crawler.family == FAMILY
    assert crawler.schema_ref == "Product@1"
    # ...and it actually runs against a fake ctx (usable, not just constructed).
    result = asyncio.run(crawler.crawl("http://x", FakeCtx()))
    assert result.items == [{"name": "Dune"}]


def test_load_specific_version_by_number(registry):
    v1 = registry.add_version(FAMILY, BOOKS_SOURCE)
    v2 = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, v2)
    # Explicit n loads that version regardless of which one is active.
    crawler = registry.load_crawler(FAMILY, v1)
    assert isinstance(crawler, Crawler)
    assert crawler.family == FAMILY


def test_load_tampered_file_is_rejected_by_ast_gate(registry, tmp_path):
    """A file edited AFTER registration to add a banned import must NOT load.

    This is the whole point of re-checking on load: ``add_version`` gated the
    source before writing, but the on-disk file is the real trust surface at load
    time, so the loader gates it again.
    """
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    # Tamper: append a banned import + call directly to the on-disk file. The
    # on-disk directory is the injective family_dir (slug + raw-name hash), not the
    # bare slug, so we derive the path the same way the registry does.
    path = tmp_path / "crawlers" / family_dir(FAMILY) / "v1.py"
    path.write_text(path.read_text() + "\nimport os\nos.getcwd()\n")
    # The gate runs on the in-memory bytes BEFORE the sha check, so a malicious
    # tamper raises ASTViolation (not IntegrityError) — the banned import is caught
    # first.
    with pytest.raises(ASTViolation):
        registry.load_crawler(FAMILY)


def test_load_missing_version_raises_clear_error(registry):
    registry.add_version(FAMILY, BOOKS_SOURCE)  # v1 exists
    with pytest.raises(LookupError):
        registry.load_crawler(FAMILY, 99)  # no v99


def test_load_with_no_active_version_raises(registry):
    registry.add_version(FAMILY, BOOKS_SOURCE)  # exists but left as 'fallback'
    # n=None resolves the ACTIVE version; there is none, so this is a clear error.
    with pytest.raises(LookupError):
        registry.load_crawler(FAMILY)


def test_load_unknown_family_raises(registry):
    with pytest.raises(LookupError):
        registry.load_crawler("nope.example.com/x")


def test_two_loads_get_independent_module_instances(registry):
    """Loading twice imports under unique module names, so a later tampered reload
    is re-parsed from disk rather than served from a cached module."""
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    a = registry.load_crawler(FAMILY)
    b = registry.load_crawler(FAMILY)
    # Distinct instances (fresh import each time), same declared identity.
    assert a is not b
    assert type(a).__module__ != type(b).__module__
    assert a.family == b.family == FAMILY


# --------------------------------------------------------------------------- #
# C1 — no TOCTOU: the gated bytes ARE the executed bytes (exactly one disk read)
# --------------------------------------------------------------------------- #

# What sits on disk in the regression test below: an UNGATED, malicious crawler
# (banned ``import os`` + a hostile identity). The two-read loader would gate the
# in-memory benign source but then re-read disk and EXECUTE this. The single-read
# loader never reads it: it runs the one in-memory string it already gated.
MALICIOUS_ON_DISK = '''\
import os
from crawloop.contract import Crawler, CrawlResult, FetchContext

LEAK = os.getcwd()


class Pwned(Crawler):
    family = "PWNED"
    schema_ref = "PWNED@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        return CrawlResult(items=[{"pwned": True}])
'''


def test_load_executes_the_gated_bytes_not_a_re_read_no_toctou(registry, monkeypatch):
    """The bytes the gate approves must be the exact bytes that execute.

    TOCTOU scenario: the file is swapped between the gate's read and the
    executor's read, so what runs was never gated. We reproduce it directly —
    the ON-DISK file holds malicious, ungated source, while a single in-memory
    read returns the benign registered source. A loader that gates one read but
    executes a *separate* disk read would run the malicious file (hostile
    identity + ``import os`` side effect). The fixed loader reads once and
    executes that same gated string, so it loads the benign crawler and the
    malicious file is never touched.
    """
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    crawler_path = tmp_path_of(registry) / family_dir(FAMILY) / "v1.py"
    # Put MALICIOUS, ungated source on disk. Only an unwanted second read would
    # ever see it.
    crawler_path.write_text(MALICIOUS_ON_DISK, encoding="utf-8")

    real_read_text = type(crawler_path).read_text
    reads = {"count": 0}

    def benign_single_read(self, *args, **kwargs):
        # The crawler file resolves to the benign REGISTERED source in memory
        # (and matches the recorded sha). Any other file passes through.
        if str(self) == str(crawler_path):
            reads["count"] += 1
            return BOOKS_SOURCE
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", benign_single_read)

    crawler = registry.load_crawler(FAMILY)
    # Exactly one read of the crawler file: the gated bytes and the executed
    # bytes come from the SAME read — there is no second disk read to swap.
    assert reads["count"] == 1
    # The benign registered crawler ran, NOT the malicious on-disk one.
    assert crawler.family == FAMILY
    assert crawler.family != "PWNED"
    result = asyncio.run(crawler.crawl("http://x", FakeCtx()))
    assert result.items == [{"name": "Dune"}]
    # The malicious file's module-level side effect never executed.
    import sys as _sys

    assert not any(
        hasattr(m, "LEAK") for m in list(_sys.modules.values()) if m is not None
    )


def test_load_clean_tamper_with_changed_bytes_raises_integrity_error(registry):
    """A file rewritten with DIFFERENT but still AST-clean bytes must not load.

    This proves the sha is actually verified (defense-in-depth, C1b): the gate
    passes the harmless edit, but the on-disk bytes no longer match the sha
    recorded at registration, so load_crawler raises IntegrityError.
    """
    n = registry.add_version(FAMILY, BOOKS_SOURCE)
    registry.set_active(FAMILY, n)
    path = tmp_path_of(registry) / family_dir(FAMILY) / "v1.py"
    # Harmless, AST-clean edit (a comment line) -> gate passes, sha differs.
    path.write_text(BOOKS_SOURCE + "\n# benign but unregistered edit\n")
    with pytest.raises(IntegrityError):
        registry.load_crawler(FAMILY)


def tmp_path_of(registry) -> "object":
    """The crawlers_dir backing a Registry (so tests can locate on-disk files
    without hardcoding the slug-vs-family_dir mapping)."""
    return registry.crawlers_dir
