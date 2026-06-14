"""Tests for the Loop's GAUNTLET step (Task 9.4): :func:`score_candidate` and
:func:`run_gauntlet`.

The gauntlet is the JUDGE of the regeneration loop (design §9). Codegen (9.2)
produced gate-passing candidate *source*; the gauntlet sandbox-runs each
candidate (9.3) against the sample pages and scores its output with the pure
validator gates (M7):

* **schema** — every sample's extraction must pass :func:`validator.validate`;
* **oracle agreement** — mean per-sample agreement with the oracle JSON must
  clear the §9 bar (>= 0.98);
* **fixture regression** — mean agreement with stored golden fixtures must be
  perfect (1.0), and a family with NO fixtures passes this vacuously;
* **exec errors** — any sandbox crash/timeout is fatal to the candidate.

A candidate ``passed`` only when ALL of those hold. :func:`run_gauntlet` scores
every candidate and returns the best PASSING one (highest oracle agreement) plus
every score (the latter feeds the round's failure report).

These tests run REAL candidate source in the REAL subprocess sandbox against
tiny hand-written listing HTML (2 books), and score with the REAL validator — so
"passed/failed" is proven end-to-end, not against stubs. No model, no network.
"""

from __future__ import annotations

from crawloop.loop.gauntlet import CandidateScore, run_gauntlet, score_candidate

# --------------------------------------------------------------------------- #
# Tiny deterministic listing HTML (2 books) + the matching oracle JSON.
#
# The sandbox runs a candidate against this HTML offline; a CORRECT books
# crawler must reproduce exactly the oracle records below. Prices are money
# strings and the detail hrefs are relative, so a correct crawler absolutizes
# them against the page URL -> the absolute URLs in _ORACLE.
# --------------------------------------------------------------------------- #

_PAGE_URL = "https://books.example.com/catalogue/page-1.html"

_LISTING_HTML = """\
<!DOCTYPE html><html><body><ol class="books">
<article class="product_pod">
  <h3><a href="a-light-in-the-attic/index.html" title="A Light in the Attic">A Light in the Attic</a></h3>
  <p class="price_color">£51.77</p>
  <p class="availability">In stock</p>
</article>
<article class="product_pod">
  <h3><a href="soumission/index.html" title="Soumission">Soumission</a></h3>
  <p class="price_color">£50.10</p>
  <p class="availability">Out of stock</p>
</article>
</ol></body></html>"""

# The oracle's trusted extraction of _LISTING_HTML (what direct_extract would
# return). prices as money strings; urls absolutized against _PAGE_URL.
_ORACLE = [
    {
        "name": "A Light in the Attic",
        "price": "51.77",
        "in_stock": True,
        "url": "https://books.example.com/catalogue/a-light-in-the-attic/index.html",
    },
    {
        "name": "Soumission",
        "price": "50.10",
        "in_stock": False,
        "url": "https://books.example.com/catalogue/soumission/index.html",
    },
]

_SAMPLES = [(_PAGE_URL, _LISTING_HTML)]
_ORACLE_JSONS = [_ORACLE]
_SCHEMA = "Product@1"

# Fixtures are replayed in the sandbox at its canonical default URL (the gauntlet
# fixture tuple is (html, expected_items) with no per-fixture URL — see the
# gauntlet docstring), so a fixture's expected `url` values are the SAME listing
# but absolutized against that default base. This is the only field affected by
# the replay URL; name/price/in_stock are URL-independent.
_SANDBOX_BASE = "https://sandbox.local/"
_FIXTURE_EXPECTED = [
    {
        "name": "A Light in the Attic",
        "price": "51.77",
        "in_stock": True,
        "url": "https://sandbox.local/a-light-in-the-attic/index.html",
    },
    {
        "name": "Soumission",
        "price": "50.10",
        "in_stock": False,
        "url": "https://sandbox.local/soumission/index.html",
    },
]


# A CORRECT books crawler: reads name/price/in_stock/url from each card exactly
# as the oracle did. Gate-clean (imports only parsel + contract).
_CORRECT = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksList(Crawler):
    family = "books.example.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            avail = ctx.clean_text(" ".join(card.css(".availability::text").getall())) or ""
            items.append({
                "name": card.css("h3 a::attr(title)").get(),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in avail,
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        return CrawlResult(items=items, next_url=None)
'''

# A WRONG-ELEMENT crawler: reads the AVAILABILITY text into `name` instead of
# the title. Still schema-valid (name is a non-empty string), but it disagrees
# with the oracle on the name field of every item -> agreement < 0.98.
_WRONG_ELEMENT = '''\
from parsel import Selector
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksWrong(Crawler):
    family = "books.example.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        html = await ctx.fetch(url)
        sel = Selector(text=html)
        items = []
        for card in sel.css("article.product_pod"):
            avail = ctx.clean_text(" ".join(card.css(".availability::text").getall())) or ""
            items.append({
                "name": ctx.clean_text(card.css(".availability::text").get()),
                "price": ctx.parse_money(card.css(".price_color::text").get()),
                "in_stock": "In stock" in avail,
                "url": ctx.absolutize(url, card.css("h3 a::attr(href)").get()),
            })
        return CrawlResult(items=items, next_url=None)
'''

# A crawler that RAISES inside crawl -> the sandbox reports a SandboxError, which
# the gauntlet counts as an exec error. Gate-clean (no banned construct); it just
# fails at runtime by indexing an empty list.
_RAISES = '''\
from crawloop.contract import Crawler, CrawlResult, FetchContext


class BooksBoom(Crawler):
    family = "books.example.com/product_list"
    schema_ref = "Product@1"

    async def crawl(self, url: str, ctx: FetchContext) -> CrawlResult:
        await ctx.fetch(url)
        boom = []
        boom[5]  # IndexError at runtime
        return CrawlResult(items=[], next_url=None)
'''


# --------------------------------------------------------------------------- #
# score_candidate
# --------------------------------------------------------------------------- #


def test_score_candidate_correct_crawler_passes():
    """A correct crawler vs the matching oracle -> schema_valid, agreement ~1.0,
    no exec errors, vacuous fixture pass (no fixtures) -> passed True."""
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert isinstance(score, CandidateScore)
    assert score.schema_valid is True
    assert score.oracle_agreement == 1.0
    assert score.exec_errors == 0
    assert score.fixture_pass == 1.0  # vacuous: no fixtures
    assert score.passed is True
    assert score.source == _CORRECT


def test_score_candidate_wrong_element_fails_on_agreement():
    """A crawler that reads the wrong field is schema-valid but disagrees with
    the oracle -> agreement below the 0.98 bar -> passed False."""
    score = score_candidate(
        _WRONG_ELEMENT, _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    # It still parses to valid Products (name is a non-empty string)...
    assert score.schema_valid is True
    # ...but the name of every item is wrong, so agreement is well under the bar.
    assert score.oracle_agreement < 0.98
    assert score.passed is False


def test_score_candidate_runtime_error_counts_exec_error():
    """A candidate that raises inside crawl -> exec_errors > 0, agreement 0,
    schema_valid False, passed False."""
    score = score_candidate(
        _RAISES, _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert score.exec_errors > 0
    assert score.oracle_agreement == 0.0
    assert score.schema_valid is False
    assert score.passed is False


def test_score_candidate_no_fixtures_is_vacuous_pass():
    """With no fixtures, fixture_pass is 1.0 (a brand-new family has nothing to
    regress against)."""
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert score.fixture_pass == 1.0


def test_score_candidate_passing_fixture_keeps_pass():
    """A fixture the candidate reproduces exactly -> fixture_pass 1.0, still
    passing. The fixture html is the same listing; expected = that listing's
    records absolutized against the sandbox replay base."""
    fixtures = [(_LISTING_HTML, _FIXTURE_EXPECTED)]
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS, fixtures=fixtures, schema_ref=_SCHEMA
    )
    assert score.fixture_pass == 1.0
    assert score.passed is True


def test_score_candidate_regressing_fixture_fails():
    """A fixture whose expected output the candidate does NOT reproduce ->
    fixture_pass < 1.0 -> passed False, even though oracle agreement is perfect.

    The fixture expects a book ("Old Title") that does not appear in the fixture
    HTML the candidate runs against, so the candidate regresses on it.
    """
    regressing_fixture_html = """\
<!DOCTYPE html><html><body><ol class="books">
<article class="product_pod">
  <h3><a href="new/index.html" title="New Title">New Title</a></h3>
  <p class="price_color">£10.00</p>
  <p class="availability">In stock</p>
</article>
</ol></body></html>"""
    expected = [
        {
            "name": "Old Title",
            "price": "99.00",
            "in_stock": True,
            "url": "https://books.example.com/catalogue/old/index.html",
        }
    ]
    fixtures = [(regressing_fixture_html, expected)]
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS, fixtures=fixtures, schema_ref=_SCHEMA
    )
    assert score.fixture_pass < 1.0
    assert score.passed is False


# --------------------------------------------------------------------------- #
# Many-item sample builder (for the per-item / item-count gate tests below).
#
# The gauntlet runs `_CORRECT` against this HTML; `_CORRECT` maps title->name,
# price_color->price (money string), availability->in_stock, href->absolutized
# url. So the records `_CORRECT` extracts from `_many_item_page(n)` are exactly
# `_many_item_oracle(n)` — a deterministic "perfect" oracle we can then perturb
# by one field on one item (to model a wrong item) or by length (to model a
# dropped item). These large samples are what dilute a MEAN below the radar: one
# wrong field across many items still averages above 0.98, which is precisely the
# C1/I1/I2 gap.
# --------------------------------------------------------------------------- #

_MANY_PAGE_URL = "https://books.example.com/catalogue/page-1.html"


def _many_item_page(n: int) -> str:
    """A listing page of ``n`` book cards in the `_CORRECT`-readable layout."""
    cards = "".join(
        f'<article class="product_pod">'
        f'<h3><a href="b{i}/index.html" title="Book {i}">Book {i}</a></h3>'
        f'<p class="price_color">£{10 + i}.00</p>'
        f'<p class="availability">In stock</p>'
        f"</article>"
        for i in range(n)
    )
    return f'<!DOCTYPE html><html><body><ol class="books">{cards}</ol></body></html>'


def _many_item_oracle(n: int) -> list[dict]:
    """The records `_CORRECT` extracts from :func:`_many_item_page` (n items)."""
    return [
        {
            "name": f"Book {i}",
            "price": f"{10 + i}.00",
            "in_stock": True,
            "url": f"https://books.example.com/catalogue/b{i}/index.html",
        }
        for i in range(n)
    ]


def test_score_candidate_one_wrong_item_in_many_fails_on_min(monkeypatch):
    """I1: a many-item sample where the candidate gets ONE field wrong on ONE
    item must FAIL — the mean agreement (~0.983) clears 0.98 and hides the error,
    so the gate must look at the lowest per-item agreement, not the mean.

    Modelled by an oracle that matches the candidate's extraction on every item
    except item[0].name. That item agrees on 5/6 fields (0.833), well under the
    bar, while the sample's mean stays above it.
    """
    html = _many_item_page(10)
    oracle = _many_item_oracle(10)
    oracle[0] = {**oracle[0], "name": "WRONG NAME"}
    score = score_candidate(
        _CORRECT, [(_MANY_PAGE_URL, html)], [oracle], fixtures=[], schema_ref=_SCHEMA
    )
    # The mean is above the bar (this is the dilution that hid the bug today)...
    assert score.oracle_agreement > 0.98
    # ...but the worst item is well below it, so the candidate must NOT pass.
    assert score.min_item_agreement < 0.98
    assert score.counts_match is True
    assert score.passed is False


def test_score_candidate_perfect_on_two_wrong_on_third_sample_fails():
    """C1: across 3 samples, perfect on 2 and one wrong item on the 3rd must
    FAIL. The 3-sample MEAN (~0.994) clears 0.98 — exactly the averaging that
    lets a wrong crawler through — so the gate must require EVERY sample's worst
    item to clear the bar (the min over all samples), not the mean.
    """
    bad_oracle = _many_item_oracle(10)
    bad_oracle[0] = {**bad_oracle[0], "name": "WRONG NAME"}
    samples = [
        (_PAGE_URL, _LISTING_HTML),
        (_PAGE_URL, _LISTING_HTML),
        (_MANY_PAGE_URL, _many_item_page(10)),
    ]
    oracles = [_ORACLE, _ORACLE, bad_oracle]
    score = score_candidate(
        _CORRECT, samples, oracles, fixtures=[], schema_ref=_SCHEMA
    )
    assert score.oracle_agreement > 0.98  # the diluted mean still clears the bar
    assert score.min_item_agreement < 0.98  # but the worst item, over all samples
    assert score.passed is False


def test_score_candidate_dropped_item_fails_on_count_match():
    """I2: a candidate that returns n-1 of n items must FAIL on the item-count
    match. Padding the missing item makes a ~100-item sample average ~0.993 —
    above the bar — so a dropped row is invisible to a mean; the gate must
    require the candidate's item count to equal the oracle's.

    Modelled by a 99-card page against a 100-record oracle (the candidate
    "dropped" one relative to the oracle).
    """
    html = _many_item_page(99)
    oracle = _many_item_oracle(100)
    score = score_candidate(
        _CORRECT, [(_MANY_PAGE_URL, html)], [oracle], fixtures=[], schema_ref=_SCHEMA
    )
    assert score.oracle_agreement > 0.98  # padded mean still clears the bar
    assert score.counts_match is False  # but a row is missing
    assert score.passed is False


def test_score_candidate_correct_crawler_reports_perfect_min_and_count_match():
    """The correct-crawler happy case still PASSES and now also reports a perfect
    worst-item agreement and a matching item count (the new gate signals)."""
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert score.passed is True
    assert score.min_item_agreement == 1.0
    assert score.counts_match is True


def test_score_candidate_fixture_dropped_item_fails_on_count_match():
    """The fixture gate is folded into the SAME count-match + per-item min
    criterion as the oracle samples: a candidate that reproduces fewer items than
    a fixture expects must FAIL on that fixture's item-count match, regardless of
    how the padded mean rounds. (Guards the fixture half of the C1/I1/I2 fix.)

    The fixture expects 2 books but the fixture HTML has only 1, so the candidate
    returns 1 of 2 — a fixture-level count mismatch.
    """
    one_book_html = """\
<!DOCTYPE html><html><body><ol class="books">
<article class="product_pod">
  <h3><a href="a-light-in-the-attic/index.html" title="A Light in the Attic">A Light in the Attic</a></h3>
  <p class="price_color">£51.77</p>
  <p class="availability">In stock</p>
</article>
</ol></body></html>"""
    score = score_candidate(
        _CORRECT, _SAMPLES, _ORACLE_JSONS,
        fixtures=[(one_book_html, _FIXTURE_EXPECTED)], schema_ref=_SCHEMA,
    )
    # Oracle samples are perfect, but the fixture is missing a row.
    assert score.passed is False


# --------------------------------------------------------------------------- #
# run_gauntlet
# --------------------------------------------------------------------------- #


def test_run_gauntlet_picks_correct_over_wrong():
    """Over [wrong, correct], the gauntlet returns the correct candidate as the
    winner and reports a score for BOTH (the loser feeds the failure report)."""
    best, all_scores = run_gauntlet(
        [_WRONG_ELEMENT, _CORRECT], _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert best is not None
    assert best.source == _CORRECT
    assert best.passed is True
    assert len(all_scores) == 2
    # both candidates are represented in the full score list
    sources = {s.source for s in all_scores}
    assert sources == {_WRONG_ELEMENT, _CORRECT}


def test_run_gauntlet_no_passing_candidate_returns_none():
    """When nothing passes, best is None but all_scores still has every score
    (so the driver can build a failure report)."""
    best, all_scores = run_gauntlet(
        [_WRONG_ELEMENT, _RAISES], _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert best is None
    assert len(all_scores) == 2
    assert all(not s.passed for s in all_scores)


def test_run_gauntlet_empty_candidates_returns_none_and_empty():
    """No candidates -> (None, [])."""
    best, all_scores = run_gauntlet(
        [], _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert best is None
    assert all_scores == []


def test_run_gauntlet_picks_highest_agreement_among_passing():
    """When several candidates pass, the winner is a passing one at the highest
    oracle agreement. Two correct candidates both pass at 1.0; the winner is a
    passing one (tie broken deterministically, first-best)."""
    best, all_scores = run_gauntlet(
        [_CORRECT, _CORRECT], _SAMPLES, _ORACLE_JSONS, fixtures=[], schema_ref=_SCHEMA
    )
    assert best is not None
    assert best.passed is True
    assert best.oracle_agreement == 1.0
    assert len(all_scores) == 2
