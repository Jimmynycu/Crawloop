"""Tests for the local controllable fixture server (Milestone 3).

The fixture server is the deterministic, offline target the whole end-to-end
crawler test rests on. These tests assert *parsed structure* (via parsel),
not just substrings, so a regression in markup shape is caught.
"""

import httpx
import pytest
from parsel import Selector

from tests.fixture_server.server import FixtureServer

# The price of the first book on page 1; used as a stable content marker that
# must survive a "mutated" redesign unchanged.
KNOWN_PRICE = "£51.77"


@pytest.fixture
def server():
    """A manually-managed server for the structural tests in this module.

    Distinct from the project-level `fixture_server` fixture (conftest.py),
    which Task 3.2 exercises separately. Always torn down, even on failure.
    """
    with FixtureServer() as srv:
        yield srv


def _selector(response: httpx.Response) -> Selector:
    return Selector(text=response.text)


# --- normal mode -----------------------------------------------------------


def test_server_starts_and_serves_page_1(server):
    resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    assert resp.status_code == 200
    sel = _selector(resp)
    assert len(sel.css("article.product_pod")) == 3
    assert sel.css("li.next")
    assert KNOWN_PRICE in resp.text


def test_root_serves_same_listing_as_page_1(server):
    root = httpx.get(f"{server.url}/")
    page1 = httpx.get(f"{server.url}/catalogue/page-1.html")
    assert root.status_code == 200
    assert len(_selector(root).css("article.product_pod")) == 3
    # Root and page-1 are the same listing.
    assert root.text == page1.text


def test_page_1_books_have_full_structure(server):
    resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    sel = _selector(resp)
    pods = sel.css("article.product_pod")
    for pod in pods:
        # title lives in the h3 > a title attribute
        assert pod.css("h3 > a::attr(title)").get()
        # relative detail href in the same anchor
        href = pod.css("h3 > a::attr(href)").get()
        assert href and href.startswith("catalogue/")
        # price + availability
        assert pod.css("p.price_color::text").get()
        assert pod.css("p.availability::text").get()


def test_page_2_has_one_book_and_no_next(server):
    resp = httpx.get(f"{server.url}/catalogue/page-2.html")
    assert resp.status_code == 200
    sel = _selector(resp)
    assert len(sel.css("article.product_pod")) == 1
    assert not sel.css("li.next")


def test_next_link_points_to_page_2(server):
    resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    href = _selector(resp).css("li.next a::attr(href)").get()
    assert href is not None
    # follow it; should resolve to the page-2 listing
    nxt = httpx.get(f"{server.url}/{href.lstrip('/')}")
    assert nxt.status_code == 200
    assert len(_selector(nxt).css("article.product_pod")) == 1


def test_unknown_path_returns_404(server):
    resp = httpx.get(f"{server.url}/catalogue/page-999.html")
    assert resp.status_code == 404


# --- mutated mode ----------------------------------------------------------


def test_mutated_mode_renames_hooks_but_keeps_data(server):
    # baseline (normal) data for the first book
    server.mode = "normal"
    normal_resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    normal_pod = _selector(normal_resp).css("article.product_pod")[0]
    normal_title = normal_pod.css("h3 > a::attr(title)").get()
    normal_price = normal_pod.css("p.price_color::text").get()

    # flip to mutated — NO restart
    server.mode = "mutated"
    mut_resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    assert mut_resp.status_code == 200
    mut_sel = _selector(mut_resp)

    # old selectors gone, new selectors present
    assert len(mut_sel.css("article.product_pod")) == 0
    assert len(mut_sel.css("div.card")) == 3
    assert mut_sel.css("span.price-box")
    assert mut_sel.css("div.pager-next")
    assert "price-box" in mut_resp.text

    # SAME data under the renamed hooks
    mut_pod = mut_sel.css("div.card")[0]
    mut_title = mut_pod.css("h3 > a::attr(title)").get()
    mut_price = mut_pod.css("span.price-box::text").get()
    assert mut_title == normal_title
    assert mut_price == normal_price
    assert normal_price == KNOWN_PRICE


def test_mutated_page_2_has_one_card_and_no_pager(server):
    server.mode = "mutated"
    resp = httpx.get(f"{server.url}/catalogue/page-2.html")
    assert resp.status_code == 200
    sel = _selector(resp)
    assert len(sel.css("div.card")) == 1
    assert not sel.css("div.pager-next")


def test_normal_and_mutated_yield_identical_book_data(server):
    """The whole point: a correct extraction is byte-identical across layouts."""

    def extract(sel: Selector, book_sel: str, price_sel: str, avail_sel: str):
        books = []
        for node in sel.css(book_sel):
            books.append(
                {
                    "title": node.css("h3 > a::attr(title)").get(),
                    "href": node.css("h3 > a::attr(href)").get(),
                    "price": node.css(f"{price_sel}::text").get(),
                    # read availability strictly through this layout's own
                    # hook; a missing hook would yield None and fail the ==.
                    "availability": node.css(f"{avail_sel}::text").get(),
                }
            )
        return books

    server.mode = "normal"
    n1 = _selector(httpx.get(f"{server.url}/catalogue/page-1.html"))
    n2 = _selector(httpx.get(f"{server.url}/catalogue/page-2.html"))
    normal_books = extract(
        n1, "article.product_pod", "p.price_color", "p.availability"
    ) + extract(n2, "article.product_pod", "p.price_color", "p.availability")

    server.mode = "mutated"
    m1 = _selector(httpx.get(f"{server.url}/catalogue/page-1.html"))
    m2 = _selector(httpx.get(f"{server.url}/catalogue/page-2.html"))
    mutated_books = extract(
        m1, "div.card", "span.price-box", "span.stock"
    ) + extract(m2, "div.card", "span.price-box", "span.stock")

    assert len(normal_books) == 4
    # every field actually resolved through its hook (no silent None==None)
    assert all(all(v is not None for v in book.values()) for book in normal_books)
    assert normal_books == mutated_books


# --- blocked mode ----------------------------------------------------------


def test_blocked_mode_returns_403_challenge(server):
    server.mode = "blocked"
    resp = httpx.get(f"{server.url}/catalogue/page-1.html")
    assert resp.status_code == 403
    assert "Attention Required" in resp.text
    assert "cf-challenge" in resp.text


def test_blocked_mode_blocks_every_route(server):
    server.mode = "blocked"
    for path in ("/", "/catalogue/page-1.html", "/catalogue/page-2.html"):
        resp = httpx.get(f"{server.url}{path}")
        assert resp.status_code == 403, path


def test_blocked_mode_bypass_header_serves_normal_markup(server):
    server.mode = "blocked"
    name, value = server.bypass_header
    resp = httpx.get(
        f"{server.url}/catalogue/page-1.html", headers={name: value}
    )
    assert resp.status_code == 200
    sel = _selector(resp)
    assert len(sel.css("article.product_pod")) == 3
    assert sel.css("li.next")


def test_blocked_mode_wrong_header_value_still_blocked(server):
    server.mode = "blocked"
    name, _ = server.bypass_header
    resp = httpx.get(
        f"{server.url}/catalogue/page-1.html", headers={name: "wrong"}
    )
    assert resp.status_code == 403


# --- bookkeeping -----------------------------------------------------------


def test_hits_records_requested_paths(server):
    httpx.get(f"{server.url}/catalogue/page-1.html")
    httpx.get(f"{server.url}/catalogue/page-2.html")
    assert "/catalogue/page-1.html" in server.hits
    assert "/catalogue/page-2.html" in server.hits


def test_default_mode_is_normal():
    srv = FixtureServer()
    assert srv.mode == "normal"
    assert srv.bypass_header == ("x-test-bypass", "ok")


# --- Task 3.2: the project-level pytest fixture ----------------------------


def test_fixture_server_fixture_serves_page_1(fixture_server):
    """Uses the conftest `fixture_server` fixture, not a manual server."""
    resp = httpx.get(f"{fixture_server.url}/catalogue/page-1.html")
    assert resp.status_code == 200
    assert fixture_server.mode == "normal"
