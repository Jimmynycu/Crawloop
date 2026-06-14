"""Task 5.3 — the Registry IS the engine's persistent AccessStore.

M4's recovery loop records which fetch strategy works per domain through the
``access.AccessStore`` port; M4 tests used an in-memory fake. M5's Registry
implements that exact port so it can be the real store. These tests pin the port
contract (``isinstance(registry, AccessStore)``) and the upsert/get behavior
against a real SQLite ``domain_access`` table.
"""

from __future__ import annotations

import pytest

from crawloop.access import AccessStore
from crawloop.registry import Registry


@pytest.fixture
def registry(tmp_path):
    return Registry(db_path=":memory:", crawlers_dir=tmp_path / "crawlers")


def test_registry_satisfies_accessstore_protocol(registry):
    # The whole point: the engine can use a Registry wherever an AccessStore is
    # expected. runtime_checkable Protocol -> isinstance verifies the surface.
    assert isinstance(registry, AccessStore)


def test_get_working_strategy_unknown_is_none(registry):
    assert registry.get_working_strategy("books.toscrape.com") is None


def test_set_then_get_working_strategy(registry):
    registry.set_working_strategy("books.toscrape.com", "stealth_browser")
    assert registry.get_working_strategy("books.toscrape.com") == "stealth_browser"


def test_set_working_strategy_upserts(registry):
    registry.set_working_strategy("books.toscrape.com", "backoff")
    registry.set_working_strategy("books.toscrape.com", "stealth_browser")
    # Second set overwrites the first (one row per domain).
    assert registry.get_working_strategy("books.toscrape.com") == "stealth_browser"


def test_mark_domain_status_persists_and_keeps_strategy(registry):
    registry.set_working_strategy("books.toscrape.com", "backoff")
    registry.mark_domain_status("books.toscrape.com", "blocked")
    # Marking status must not wipe an already-known working strategy.
    assert registry.get_working_strategy("books.toscrape.com") == "backoff"
    assert registry._domain_access_row("books.toscrape.com")["status"] == "blocked"


def test_mark_domain_status_before_any_strategy(registry):
    # mark_domain_status on a brand-new domain creates the row (strategy NULL).
    registry.mark_domain_status("new.example.com", "healthy")
    row = registry._domain_access_row("new.example.com")
    assert row["status"] == "healthy"
    assert row["working_strategy"] is None
    assert registry.get_working_strategy("new.example.com") is None


def test_now_param_is_recorded(registry):
    registry.set_working_strategy("d.example.com", "plain", now="2026-06-13T12:00:00+00:00")
    assert registry._domain_access_row("d.example.com")["updated_at"] == (
        "2026-06-13T12:00:00+00:00"
    )
