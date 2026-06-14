"""OPTIONAL live integration test for :class:`PlaywrightBrowserRunner`.

This is the ONE test that launches a real Chromium. It is:

* marked ``@pytest.mark.browser`` (registered in ``conftest.py`` via
  ``pytest_configure`` -> ``addinivalue_line``), and
* skipped unless ``RUN_BROWSER_TESTS=1`` is set in the environment.

So a default ``pytest`` run never starts a browser and never needs one installed.
Run it explicitly with::

    RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_browser_live.py -q

What it proves:

1. **A real render runs JS.** A tiny inline HTTP server serves a page whose
   visible text is injected by a ``<script>`` at load time. The raw HTML does NOT
   contain that text, so finding it in ``render()``'s output proves the page was
   actually executed by a browser, not just fetched.
2. **The allowlist is enforced on navigation.** Navigating to a host that is not
   on the allowlist raises :class:`~crawloop.config.UnauthorizedDomain`.
3. **A JS redirect to an off-list host is aborted, not followed.** A page on the
   authorized host that does ``location.href = "http://<off-list>/"`` must not
   deliver the off-list body; the render fails instead.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawloop.config import AppConfig, DomainConfig, UnauthorizedDomain

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("RUN_BROWSER_TESTS") != "1",
        reason="live browser test; set RUN_BROWSER_TESTS=1 to run",
    ),
]

# Text injected purely by JS at load — absent from the served HTML source, so its
# presence in the rendered output proves a real browser executed the script.
JS_INJECTED_TEXT = "RENDERED_BY_JS_8F3A2C"

_PAGE_JS_INJECT = (
    "<!DOCTYPE html><html><head><title>live</title></head><body>"
    '<div id="target">PLACEHOLDER</div>'
    "<script>document.getElementById('target').textContent = "
    f"'{JS_INJECTED_TEXT}';</script>"
    "</body></html>"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    """A localhost ThreadingHTTPServer serving two routes:

    * ``/js`` — the JS-injection page above.
    * ``/redirect-evil`` — a page that JS-redirects to ``http://<offhost>/``,
      used to prove a cross-host in-page redirect is aborted.
    """

    def __init__(self, offhost_netloc: str) -> None:
        self._offhost_netloc = offhost_netloc
        self.port = _free_port()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> _LiveServer:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
                if self.path == "/js":
                    body = _PAGE_JS_INJECT
                elif self.path == "/redirect-evil":
                    body = (
                        "<!DOCTYPE html><html><body>bouncing"
                        f"<script>location.href = 'http://{server._offhost_netloc}/';"
                        "</script></body></html>"
                    )
                else:
                    body = "<!DOCTYPE html><html><body>home</body></html>"
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


@pytest.fixture
def live_server():
    # The off-list host the redirect page points at: a different loopback address
    # (127.0.0.2) that is deliberately NOT in the allowlist.
    off_port = _free_port()
    server = _LiveServer(offhost_netloc=f"127.0.0.2:{off_port}").start()
    try:
        yield server
    finally:
        server.stop()


def _config_authorizing(host: str) -> AppConfig:
    return AppConfig(
        respect_robots=False,
        domains={host: DomainConfig(domain=host, max_rps=100.0, render_js=True)},
    )


async def test_live_render_executes_js(live_server):
    """A real render runs the page's script; the JS-injected text appears in the
    returned HTML (it is absent from the served source)."""
    from crawloop.browser import PlaywrightBrowserRunner

    cfg = _config_authorizing("127.0.0.1")
    runner = PlaywrightBrowserRunner(cfg, headless=True, timeout_ms=15_000)
    try:
        html = await runner.render(
            f"{live_server.url}/js", stealth=False, wait_for="#target"
        )
    finally:
        await runner.aclose()
    assert JS_INJECTED_TEXT in html


async def test_live_offlist_navigation_raises(live_server):
    """Navigating to a host that is not on the allowlist raises
    UnauthorizedDomain (the up-front gate; no off-list body is ever returned)."""
    from crawloop.browser import PlaywrightBrowserRunner

    cfg = _config_authorizing("127.0.0.1")  # only 127.0.0.1 authorized
    runner = PlaywrightBrowserRunner(cfg, headless=True, timeout_ms=15_000)
    try:
        with pytest.raises(UnauthorizedDomain):
            # 127.0.0.2 is a different host, not on the allowlist.
            await runner.render("http://127.0.0.2:9/", stealth=False)
    finally:
        await runner.aclose()


async def test_live_js_redirect_to_offlist_is_aborted(live_server):
    """A page on the authorized host that JS-redirects to an off-list host must
    NOT follow it. The route handler aborts the off-list main-frame navigation
    *before it leaves the browser* (so it works even if the off-list host is
    unreachable), the main frame ends on a chrome-error page, and the frame check
    raises :class:`UnauthorizedDomain` — the off-list body is never returned."""
    from crawloop.browser import PlaywrightBrowserRunner

    cfg = _config_authorizing("127.0.0.1")  # 127.0.0.2 (redirect target) is off-list
    runner = PlaywrightBrowserRunner(cfg, headless=True, timeout_ms=15_000)
    try:
        with pytest.raises(UnauthorizedDomain):
            await runner.render(f"{live_server.url}/redirect-evil", stealth=False)
    finally:
        await runner.aclose()
