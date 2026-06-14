from decimal import Decimal

import pytest
from pydantic import ValidationError

from crawloop.contract import (
    Crawler,
    CrawlResult,
    FetchContext,
    absolutize,
    clean_text,
    parse_money,
)


class FakeCrawler:
    family = "x.com/list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        return CrawlResult(items=[])


def test_fake_crawler_satisfies_protocol():
    assert isinstance(FakeCrawler(), Crawler)


def test_crawl_result_defaults_next_url():
    result = CrawlResult(items=[{"name": "A"}])
    assert result.items == [{"name": "A"}]
    assert result.next_url is None


def test_crawl_result_rejects_bad_items():
    with pytest.raises(ValidationError):
        CrawlResult(items="nope")


def test_absolutize():
    assert absolutize("https://x.com/a/b", "../c") == "https://x.com/c"


def test_absolutize_none_href():
    assert absolutize("https://x.com/a/b", None) is None


def test_parse_money_currency_symbol():
    assert parse_money("£51.77") == Decimal("51.77")


def test_parse_money_no_number():
    assert parse_money("Free shipping") is None


def test_clean_text():
    assert clean_text("  a\n  b ") == "a b"
