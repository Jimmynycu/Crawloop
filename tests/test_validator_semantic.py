"""Gate 3 (oracle agreement), Gate 4 (fixture regression), Gate 5 (history).

All three semantic gates run over *already-extracted* item lists — pure
comparison, no fetching, no crawler execution. Gates 3 and 4 share one engine
(:func:`items_agreement`); :func:`field_equal` is the single value comparator.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crawloop.validator import (
    AgreementDetail,
    agreement_detail,
    field_equal,
    fixture_regression,
    history_crosscheck,
    items_agreement,
    oracle_agreement,
)


def _product(**overrides) -> dict:
    item = {
        "name": "Widget",
        "price": "12.50",
        "currency": "GBP",
        "in_stock": True,
        "url": "https://shop.example.com/widget",
        "image_url": None,
    }
    item.update(overrides)
    return item


# -- field_equal -------------------------------------------------------------- #


def test_field_equal_stable_string_exact_after_normalization():
    # Whitespace is collapsed/stripped before comparing stable strings.
    assert field_equal("  Hello   World ", "Hello World", volatile=False) is True


def test_field_equal_stable_string_differs():
    assert field_equal("Hello", "Goodbye", volatile=False) is False


def test_field_equal_money_symbol_vs_plain():
    # Money-ish strings compare by numeric value.
    assert field_equal("£51.77", "51.77", volatile=False) is True


def test_field_equal_money_string_vs_decimal_volatile():
    # The volatile numeric path still normalizes "£51.77" == Decimal("51.77").
    assert field_equal("£51.77", Decimal("51.77"), volatile=True) is True


def test_field_equal_bool_exact():
    assert field_equal(True, True, volatile=False) is True
    assert field_equal(True, False, volatile=False) is False


def test_field_equal_both_none_equal():
    assert field_equal(None, None, volatile=False) is True


# -- items_agreement (Gate 3/4 engine) --------------------------------------- #


def test_identical_items_agree_fully():
    items = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
        _product(name="C", url="https://shop.example.com/c"),
    ]
    other = [dict(i) for i in items]
    assert items_agreement(items, other, "Product@1") == 1.0


def test_money_normalization_keeps_agreement_full():
    # candidate price "£51.77" vs oracle Decimal("51.77") -> still 1.0.
    cand = [_product(name="A", url="https://shop.example.com/a", price="£51.77")]
    oracle = [_product(name="A", url="https://shop.example.com/a", price=Decimal("51.77"))]
    assert items_agreement(cand, oracle, "Product@1") == 1.0


def test_both_empty_agree():
    assert items_agreement([], [], "Product@1") == 1.0


def test_one_empty_disagrees():
    assert items_agreement([_product()], [], "Product@1") == 0.0
    assert items_agreement([], [_product()], "Product@1") == 0.0


def test_wrong_element_one_field_lowers_agreement_below_one():
    # The wrong-element-right-type signal: one item's `name` was read from the
    # wrong DOM node, so it disagrees on exactly one field. 3 items x 6 fields =
    # 18 comparisons, 1 disagreement -> 17/18 ~= 0.944, between 0.9 and 1.0.
    oracle = [
        _product(name="Real A", url="https://shop.example.com/a"),
        _product(name="Real B", url="https://shop.example.com/b"),
        _product(name="Real C", url="https://shop.example.com/c"),
    ]
    cand = [
        _product(name="Real A", url="https://shop.example.com/a"),
        _product(name="Sidebar Promo", url="https://shop.example.com/b"),  # wrong node
        _product(name="Real C", url="https://shop.example.com/c"),
    ]
    score = items_agreement(cand, oracle, "Product@1")
    assert 0.9 < score < 1.0


def test_fully_wrong_candidate_scores_low():
    oracle = [
        _product(name="Real A", price="10.00", url="https://shop.example.com/a", in_stock=True),
        _product(name="Real B", price="20.00", url="https://shop.example.com/b", in_stock=True),
    ]
    cand = [
        _product(name="X", price="999.00", url="https://wrong.example.com/z", in_stock=False),
        _product(name="Y", price="888.00", url="https://wrong.example.com/w", in_stock=False),
    ]
    score = items_agreement(cand, oracle, "Product@1")
    assert score < 0.5


def test_missing_item_on_one_side_counts_as_disagreement():
    # Shorter list padded; the unmatched item disagrees on every field.
    oracle = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    cand = [_product(name="A", url="https://shop.example.com/a")]
    score = items_agreement(cand, oracle, "Product@1")
    assert 0.0 < score < 1.0  # first item agrees, second is missing on candidate


# -- oracle_agreement / fixture_regression (thin aliases) -------------------- #


def test_oracle_agreement_matches_items_agreement():
    cand = [_product(name="A", url="https://shop.example.com/a")]
    oracle = [_product(name="A", url="https://shop.example.com/a")]
    assert oracle_agreement(cand, oracle, "Product@1") == items_agreement(
        cand, oracle, "Product@1"
    )


def test_fixture_regression_mirrors_oracle_on_stored_expected():
    expected = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    actual_good = [dict(i) for i in expected]
    assert fixture_regression(actual_good, expected, "Product@1") == 1.0

    actual_bad = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="WRONG", url="https://shop.example.com/b"),
    ]
    assert fixture_regression(actual_bad, expected, "Product@1") < 1.0


# -- agreement_detail (per-item gate view) ----------------------------------- #


def test_agreement_detail_perfect_match_is_one_and_count_matches():
    """Identical lists -> mean 1.0, min_item 1.0, count_match True, n_items set."""
    items = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    detail = agreement_detail([dict(i) for i in items], items, "Product@1")
    assert isinstance(detail, AgreementDetail)
    assert detail.mean == 1.0
    assert detail.min_item == 1.0
    assert detail.count_match is True
    assert detail.n_items == 2


def test_agreement_detail_one_wrong_item_drops_min_not_just_mean():
    """One field wrong on one of two items: the mean stays high but min_item
    drops to that item's fraction — the value the gauntlet gates on."""
    oracle = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    actual = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="WRONG", url="https://shop.example.com/b"),
    ]
    detail = agreement_detail(actual, oracle, "Product@1")
    # 6 fields/item: the bad item agrees on 5/6, the good item on 6/6.
    assert detail.min_item == 5 / 6
    assert detail.mean == (1.0 + 5 / 6) / 2
    assert detail.mean > detail.min_item  # the mean hides the worst item
    assert detail.count_match is True


def test_agreement_detail_dropped_item_flags_count_mismatch():
    """A candidate returning fewer items than the oracle -> count_match False,
    and the padded missing row is the worst item (well below the bar)."""
    oracle = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    actual = [_product(name="A", url="https://shop.example.com/a")]
    detail = agreement_detail(actual, oracle, "Product@1")
    assert detail.count_match is False
    assert detail.n_items == 2
    # The unmatched (padded {}) row disagrees on every field the oracle fills;
    # only image_url (None on both sides) agrees -> 1/6, far under the 0.98 bar.
    assert detail.min_item == 1 / 6
    assert detail.min_item < 0.98


def test_agreement_detail_both_empty_is_perfect_and_matches():
    """Both empty -> a vacuous perfect, count-matching detail."""
    detail = agreement_detail([], [], "Product@1")
    assert detail.mean == 1.0
    assert detail.min_item == 1.0
    assert detail.count_match is True
    assert detail.n_items == 0


def test_agreement_detail_one_empty_is_zero_and_count_mismatch():
    """Exactly one side empty -> 0.0 agreement and a count mismatch."""
    one = [_product(name="A", url="https://shop.example.com/a")]
    d_actual_empty = agreement_detail([], one, "Product@1")
    assert d_actual_empty.min_item == 0.0
    assert d_actual_empty.mean == 0.0
    assert d_actual_empty.count_match is False
    assert d_actual_empty.n_items == 1

    d_oracle_empty = agreement_detail(one, [], "Product@1")
    assert d_oracle_empty.min_item == 0.0
    assert d_oracle_empty.count_match is False


def test_agreement_detail_mean_matches_items_agreement_reporting():
    """The reported mean equals items_agreement (one comparison engine, two views
    — DRY): constant field count means per-item-mean == pooled mean."""
    oracle = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    actual = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="WRONG", url="https://shop.example.com/b"),
    ]
    assert agreement_detail(actual, oracle, "Product@1").mean == pytest.approx(
        items_agreement(actual, oracle, "Product@1")
    )


# -- history_crosscheck (Gate 5) --------------------------------------------- #


def _history(items: list[dict]) -> list[dict]:
    # Mirrors registry.recent_history rows: newest-first, each with an 'items'.
    return [{"items": items}]


def test_history_small_price_move_no_warning():
    prior = [_product(name="A", url="https://shop.example.com/a", price="50.00")]
    current = [_product(name="A", url="https://shop.example.com/a", price="51.00")]
    warnings = history_crosscheck(current, _history(prior), "Product@1")
    assert warnings == []


def test_history_large_price_jump_warns():
    prior = [_product(name="A", url="https://shop.example.com/a", price="50.00")]
    current = [_product(name="A", url="https://shop.example.com/a", price="200.00")]
    warnings = history_crosscheck(current, _history(prior), "Product@1")
    assert warnings  # non-empty
    assert any("price" in w for w in warnings)


def test_history_nonvolatile_change_no_warning():
    # name is NOT volatile, so even a total change emits no jump warning.
    prior = [_product(name="Old Name", url="https://shop.example.com/a", price="50.00")]
    current = [_product(name="New Name", url="https://shop.example.com/a", price="50.00")]
    warnings = history_crosscheck(current, _history(prior), "Product@1")
    assert warnings == []


def test_history_empty_returns_no_warnings():
    current = [_product(name="A", url="https://shop.example.com/a", price="50.00")]
    assert history_crosscheck(current, [], "Product@1") == []
