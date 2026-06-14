from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin

from pydantic import BaseModel


class CrawlResult(BaseModel):
    items: list[dict]
    next_url: str | None = None


def absolutize(base: str, href: str | None) -> str | None:
    return urljoin(base, href) if href else None


_MONEY_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def parse_money(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    m = _MONEY_RE.search(raw.replace(",", ""))
    if not m:
        return None
    try:
        return Decimal(m.group())
    except InvalidOperation:
        return None


def clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    return re.sub(r"\s+", " ", raw).strip()


@runtime_checkable
class FetchContext(Protocol):
    async def fetch(self, url: str) -> str: ...
    async def fetch_rendered(self, url: str, wait_for: str | None = None) -> str: ...
    def absolutize(self, base: str, href: str | None) -> str | None: ...
    def parse_money(self, raw: str | None) -> Decimal | None: ...
    def clean_text(self, raw: str | None) -> str | None: ...


@runtime_checkable
class Crawler(Protocol):
    family: str
    schema_ref: str
    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult: ...
