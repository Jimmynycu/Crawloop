"""Project-wide pytest fixtures."""

import pytest

from tests.fixture_server.server import FixtureServer


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers here (not in pyproject) so adding one needs no
    edit to the packaging config.

    ``browser`` marks the OPTIONAL live integration test that launches a real
    Chromium (``tests/test_browser_live.py``); it is also skipped unless
    ``RUN_BROWSER_TESTS=1``, so a default ``pytest`` run never touches a browser.
    """
    config.addinivalue_line(
        "markers",
        "browser: live test that launches a real browser (set RUN_BROWSER_TESTS=1)",
    )


@pytest.fixture
def fixture_server():
    """A started :class:`FixtureServer` in ``normal`` mode, per test.

    Function-scoped so each test gets a clean ``mode``/``hits``. Always torn
    down in teardown so no background threads or ports leak, even on failure.
    """
    server = FixtureServer()
    server.start()
    server.mode = "normal"
    try:
        yield server
    finally:
        server.stop()
