"""Tests for the CLI (Task 11.1): :func:`crawloop.cli.main`.

The CLI is a THIN wrapper over the engine + registry: it parses argv, builds the
shared deps from ``--config``/``--db``/``--crawlers-dir``/``--fixtures-dir``, and
dispatches to a subcommand. These tests exercise (a) argument parsing and the
error paths (unknown subcommand, missing args -> non-zero + usage), and (b) the
read-only commands (``family``/``access``/``audit``) against a pre-seeded
registry. The one command that would touch a live model/network — ``crawl`` —
is tested only for arg threading, with ``engine.request`` monkeypatched, so NO
test here ever reaches a real model or the network.
"""

from __future__ import annotations

import json

import pytest

from crawloop import cli
from crawloop.engine import RequestResult
from crawloop.registry import Registry

SCHEMA = "Product@1"
FAMILY = "books.toscrape.com/product_list"
PATTERN = r"^https?://books\.toscrape\.com/.*"

# A tiny gate-clean crawler so add_version (which AST-gates) accepts it; the CLI
# read commands only need a registered version to display, never to run.
_CRAWLER = '''\
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksList(Crawler):
    family = "books.toscrape.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        return CrawlResult(items=[], next_url=None)
'''


@pytest.fixture
def seeded(tmp_path):
    """A registry with one family + an active version, an audit entry, and an
    access-store row — enough for every read-only command to print something.

    Returns ``(db_path, crawlers_dir)`` as strings so the CLI can re-open the
    SAME on-disk SQLite file in a fresh invocation.
    """
    db_path = tmp_path / "registry.db"
    crawlers_dir = tmp_path / "crawlers"
    registry = Registry(db_path=str(db_path), crawlers_dir=crawlers_dir)
    registry.upsert_family(FAMILY, [PATTERN], SCHEMA, now="2026-06-13T00:00:00+00:00")
    registry.add_version(FAMILY, _CRAWLER, now="2026-06-13T00:00:00+00:00")
    registry.set_active(FAMILY, 1)
    registry.write_audit(
        "promote",
        family=FAMILY,
        data={"to_version": 1, "schema_ref": SCHEMA},
        now="2026-06-13T00:00:00+00:00",
    )
    registry.set_working_strategy(
        "books.toscrape.com", "backoff", now="2026-06-13T00:00:00+00:00"
    )
    return str(db_path), str(crawlers_dir)


def _common_args(seeded: tuple[str, str]) -> list[str]:
    db_path, crawlers_dir = seeded
    return ["--db", db_path, "--crawlers-dir", crawlers_dir]


# --------------------------------------------------------------------------- #
# family list / show
# --------------------------------------------------------------------------- #


def test_family_list_prints_family_and_returns_zero(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "family", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert FAMILY in out


def test_family_list_json(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "family", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["family"] == FAMILY for entry in payload)


def test_family_show_prints_version_ladder_and_status(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "family", "show", FAMILY])
    assert rc == 0
    out = capsys.readouterr().out
    # The ladder rung (v1, active) and the family status are both shown.
    assert "active" in out
    assert "1" in out


def test_family_show_unknown_returns_nonzero(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "family", "show", "no.such/family"])
    assert rc != 0


# --------------------------------------------------------------------------- #
# access status
# --------------------------------------------------------------------------- #


def test_access_status_prints_working_strategy(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "access", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "books.toscrape.com" in out
    assert "backoff" in out


def test_access_status_json(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "access", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(
        row["domain"] == "books.toscrape.com" and row["working_strategy"] == "backoff"
        for row in payload
    )


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


def test_audit_prints_promote_event(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "audit"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "promote" in out


def test_audit_filtered_by_family(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "audit", FAMILY])
    assert rc == 0
    out = capsys.readouterr().out
    assert "promote" in out


def test_audit_json(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "audit", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entry["event"] == "promote" for entry in payload)


# --------------------------------------------------------------------------- #
# error paths: unknown subcommand / missing args -> non-zero + usage
# --------------------------------------------------------------------------- #


def test_no_subcommand_returns_nonzero_with_usage(capsys):
    rc = cli.main([])
    assert rc != 0
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_unknown_subcommand_returns_nonzero(capsys):
    # argparse exits with code 2 on an invalid choice; main() surfaces that as a
    # non-zero return rather than letting SystemExit escape.
    rc = cli.main(["frobnicate"])
    assert rc != 0


def test_crawl_missing_url_returns_nonzero(capsys):
    rc = cli.main(["crawl"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_family_missing_subcommand_returns_nonzero(seeded):
    rc = cli.main([*_common_args(seeded), "family"])
    assert rc != 0


# --------------------------------------------------------------------------- #
# crawl: arg parsing threads through to engine.request (monkeypatched — no model)
# --------------------------------------------------------------------------- #


def test_crawl_parses_schema_and_json_and_threads_to_request(
    seeded, capsys, monkeypatch
):
    """``crawl --json --schema Product@1 <url>`` builds an engine and calls
    ``engine.request(url, schema="Product@1")``; the parsed args reach request().

    ``Engine.request`` is monkeypatched to a stub that records the call and
    returns a canned RequestResult, so no live crawl, model, or network happens.
    ``--offline`` keeps the build helper from constructing a real
    LiteLLMCompleter / browser.
    """
    captured = {}

    async def fake_request(self, url, *, schema=None):
        captured["url"] = url
        captured["schema"] = schema
        return RequestResult(
            items=[{"name": "X"}],
            source="registry",
            family=FAMILY,
            used_version=1,
            reason="ok",
        )

    monkeypatch.setattr("crawloop.engine.Engine.request", fake_request)

    url = "https://books.toscrape.com/catalogue/page-1.html"
    rc = cli.main(
        [*_common_args(seeded), "crawl", "--json", "--schema", SCHEMA, url, "--offline"]
    )
    assert rc == 0
    assert captured["url"] == url
    assert captured["schema"] == SCHEMA
    # --json -> the printed output is the result as JSON with the key fields.
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "registry"
    assert payload["family"] == FAMILY
    assert payload["used_version"] == 1
    assert payload["count"] == 1


def test_crawl_text_output_threads_url_without_schema(seeded, capsys, monkeypatch):
    """Plain (non-JSON) ``crawl <url>`` reaches request() with schema=None and
    prints a readable summary line including the source and family."""
    captured = {}

    async def fake_request(self, url, *, schema=None):
        captured["url"] = url
        captured["schema"] = schema
        return RequestResult(
            items=[], source="bootstrap", family=FAMILY, reason="new family"
        )

    monkeypatch.setattr("crawloop.engine.Engine.request", fake_request)

    url = "https://books.toscrape.com/catalogue/page-1.html"
    rc = cli.main([*_common_args(seeded), "crawl", url, "--offline"])
    assert rc == 0
    assert captured["url"] == url
    assert captured["schema"] is None
    out = capsys.readouterr().out
    assert "bootstrap" in out
    assert FAMILY in out


# --------------------------------------------------------------------------- #
# loop run: arg parsing threads family + seeds to run_loop (monkeypatched)
# --------------------------------------------------------------------------- #


def test_loop_run_threads_family_and_seeds(seeded, capsys, monkeypatch):
    """``loop run <family> <seed_url...>`` builds an engine and calls run_loop
    with the family + every seed url; the LoopResult is printed. run_loop is
    monkeypatched so no real sampling/codegen/model happens."""
    from crawloop.loop.driver import LoopResult

    captured = {}

    async def fake_run_loop(family, seed_urls, *args, **kwargs):
        captured["family"] = family
        captured["seeds"] = list(seed_urls)
        return LoopResult(
            ok=True, version=2, rounds=1, escalated=False, reason="promoted"
        )

    monkeypatch.setattr("crawloop.cli.run_loop", fake_run_loop)

    rc = cli.main(
        [
            *_common_args(seeded),
            "loop",
            "run",
            FAMILY,
            "https://books.toscrape.com/catalogue/page-1.html",
            "https://books.toscrape.com/catalogue/page-2.html",
            "--offline",
        ]
    )
    assert rc == 0
    assert captured["family"] == FAMILY
    assert captured["seeds"] == [
        "https://books.toscrape.com/catalogue/page-1.html",
        "https://books.toscrape.com/catalogue/page-2.html",
    ]
    out = capsys.readouterr().out
    assert "promoted" in out


def test_loop_run_missing_seeds_returns_nonzero(seeded, capsys):
    rc = cli.main([*_common_args(seeded), "loop", "run", FAMILY])
    assert rc != 0
    err = capsys.readouterr().err
    assert "usage" in err.lower()
