"""A controllable, localhost-only HTTP server used as a deterministic crawl
target across the crawloop test suite.

It can serve a books listing in three modes, switchable at runtime with no
restart (the handler reads ``server.mode`` per request):

* ``normal``  — markup mirroring books.toscrape.com (``article.product_pod``...).
* ``mutated`` — the SAME book data under renamed CSS hooks (simulating a
  redesign that breaks a brittle crawler). A *correct* extraction yields
  byte-identical JSON in both modes.
* ``blocked`` — every route returns a 403 Cloudflare-ish challenge UNLESS the
  request carries the bypass header, in which case it behaves like ``normal``.

There is ONE source of book data (``_BOOKS``); both layouts are produced by a
single parameterised renderer driven by a per-mode :class:`_Layout`, so the
modes can never drift apart in content.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

Mode = Literal["normal", "mutated", "blocked"]

# --- single source of truth: 4 books -------------------------------------- #
# 3 land on page 1, the 4th on page 2. ``href`` is relative to the catalogue
# root, mirroring books.toscrape.com detail links.
_BOOKS: list[dict[str, str]] = [
    {
        "title": "A Light in the Attic",
        "price": "£51.77",
        "availability": "In stock",
        "href": "catalogue/a-light-in-the-attic/index.html",
    },
    {
        "title": "Tipping the Velvet",
        "price": "£53.74",
        "availability": "In stock",
        "href": "catalogue/tipping-the-velvet/index.html",
    },
    {
        "title": "Soumission",
        "price": "£50.10",
        "availability": "Out of stock",
        "href": "catalogue/soumission/index.html",
    },
    {
        "title": "Sharp Objects",
        "price": "£47.82",
        "availability": "In stock",
        "href": "catalogue/sharp-objects/index.html",
    },
]

_PAGE_1_BOOKS = _BOOKS[:3]
_PAGE_2_BOOKS = _BOOKS[3:]

_CHALLENGE_BODY = (
    "<html><head><title>Attention Required!</title></head>"
    '<body><div class="cf-challenge">Attention Required! '
    "This site is protected. Verification required.</div></body></html>"
)


@dataclass(frozen=True)
class _Layout:
    """The CSS/HTML hooks that distinguish one rendered layout from another.

    Same meaning, different selectors — this is the only thing that differs
    between ``normal`` and ``mutated``.
    """

    book_tag: str
    book_class: str
    price_tag: str
    price_class: str
    avail_tag: str
    avail_class: str
    next_tag: str
    next_class: str


_NORMAL_LAYOUT = _Layout(
    book_tag="article",
    book_class="product_pod",
    price_tag="p",
    price_class="price_color",
    avail_tag="p",
    avail_class="availability",
    next_tag="li",
    next_class="next",
)

_MUTATED_LAYOUT = _Layout(
    book_tag="div",
    book_class="card",
    price_tag="span",
    price_class="price-box",
    avail_tag="span",
    avail_class="stock",
    next_tag="div",
    next_class="pager-next",
)


def _render_listing(
    books: list[dict[str, str]],
    layout: _Layout,
    next_href: str | None,
) -> str:
    """Render a listing page for ``books`` using ``layout``'s hooks.

    Single rendering routine for both modes: the layout differences are passed
    in as data, never branched across separate functions, so the two modes
    cannot diverge in content.
    """
    cards = []
    for book in books:
        cards.append(
            f'<{layout.book_tag} class="{layout.book_class}">'
            f'<h3><a href="{book["href"]}" title="{book["title"]}">'
            f"{book['title']}</a></h3>"
            f'<{layout.price_tag} class="{layout.price_class}">'
            f"{book['price']}</{layout.price_tag}>"
            f'<{layout.avail_tag} class="{layout.avail_class}">'
            f"{book['availability']}</{layout.avail_tag}>"
            f"</{layout.book_tag}>"
        )

    next_html = ""
    if next_href is not None:
        next_html = (
            f'<{layout.next_tag} class="{layout.next_class}">'
            f'<a href="{next_href}">next</a></{layout.next_tag}>'
        )

    return (
        "<!DOCTYPE html><html><head><title>Books</title></head><body>"
        '<section><ol class="books">'
        f"{''.join(cards)}"
        "</ol>"
        f'<ul class="pager">{next_html}</ul>'
        "</section></body></html>"
    )


# Routes that serve a listing → (books on that page, href of the next page).
_LISTING_ROUTES: dict[str, tuple[list[dict[str, str]], str | None]] = {
    "/": (_PAGE_1_BOOKS, "/catalogue/page-2.html"),
    "/catalogue/page-1.html": (_PAGE_1_BOOKS, "/catalogue/page-2.html"),
    "/catalogue/page-2.html": (_PAGE_2_BOOKS, None),
}

_LAYOUTS: dict[Mode, _Layout] = {
    "normal": _NORMAL_LAYOUT,
    "mutated": _MUTATED_LAYOUT,
}


def _make_handler(server: FixtureServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr request logging during tests.
        def log_message(self, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            server.hits.append(self.path)
            mode = server.mode

            # blocked mode short-circuits everything unless bypassed.
            if mode == "blocked" and not self._has_bypass():
                self._send(403, _CHALLENGE_BODY)
                return

            route = _LISTING_ROUTES.get(self.path)
            if route is None:
                self._send(404, "<html><body>Not found</body></html>")
                return

            books, next_href = route
            # blocked+bypass behaves exactly as normal.
            layout = _LAYOUTS["normal" if mode == "blocked" else mode]
            self._send(200, _render_listing(books, layout, next_href))

        def _has_bypass(self) -> bool:
            name, value = server.bypass_header
            return self.headers.get(name) == value

        def _send(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class FixtureServer:
    """A ThreadingHTTPServer on 127.0.0.1:0 (ephemeral port) in a daemon thread.

    Usable as a context manager. ``mode`` and ``bypass_header`` are mutable and
    read per-request, so behaviour can be flipped mid-test with no restart.
    """

    def __init__(
        self,
        mode: Mode = "normal",
        bypass_header: tuple[str, str] = ("x-test-bypass", "ok"),
    ) -> None:
        self.mode: Mode = mode
        self.bypass_header = bypass_header
        self.hits: list[str] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------- #

    def start(self) -> FixtureServer:
        if self._httpd is not None:
            return self
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        self._wait_until_accepting()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- properties ------------------------------------------------------- #

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- internals -------------------------------------------------------- #

    def _wait_until_accepting(self, timeout: float = 5.0) -> None:
        """Block until the port accepts a TCP connection (or time out)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                try:
                    sock.connect(("127.0.0.1", self.port))
                    return
                except OSError:
                    time.sleep(0.01)
        raise RuntimeError("fixture server did not start accepting in time")
