"""Gate 1 (schema coercion) + Gate 2 (field-fill floors) + item-count floor.

All cases use ``Product@1`` (``name``, ``price``, ``in_stock``, ``url`` required;
``currency``, ``image_url`` optional). ``price`` is required, so a present-but-None
``price`` drops its fill_rate and trips Gate 2.
"""

from __future__ import annotations

from crawloop.validator import ValidationReport, validate


def _product(**overrides) -> dict:
    """A schema-valid Product@1 item; override individual keys per case."""
    item = {
        "name": "Widget",
        "price": "12.50",
        "in_stock": True,
        "url": "https://shop.example.com/widget",
    }
    item.update(overrides)
    return item


def test_three_valid_products_pass_all_gates():
    items = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
        _product(name="C", url="https://shop.example.com/c"),
    ]
    report = validate(items, "Product@1")
    assert isinstance(report, ValidationReport)
    assert report.ok is True
    assert report.reason == ""
    assert report.failures == []
    assert report.item_count == 3
    # Every required field is present and non-None on all three.
    for field in ("name", "price", "in_stock", "url"):
        assert report.fill_rates[field] == 1.0


def test_report_satisfies_executor_validationlike():
    # The executor only reads .ok and .reason; prove both exist on the report.
    report = validate([_product()], "Product@1")
    assert hasattr(report, "ok")
    assert hasattr(report, "reason")
    assert isinstance(report.ok, bool)
    assert isinstance(report.reason, str)


def test_wrong_type_trips_schema_gate():
    items = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b", price="Free shipping"),
        _product(name="C", url="https://shop.example.com/c"),
    ]
    report = validate(items, "Product@1")
    assert report.ok is False
    assert report.reason.startswith("schema")
    assert report.failures  # non-empty
    # The failing index is reported.
    assert any("item[1]" in f for f in report.failures)


def test_extra_field_trips_schema_gate_via_extra_forbid():
    items = [_product(junk="surprise")]
    report = validate(items, "Product@1")
    assert report.ok is False
    assert report.reason.startswith("schema")
    assert report.failures


def test_schema_reason_includes_invalid_count():
    items = [
        _product(price="nope"),
        _product(price="nope"),
        _product(),
        _product(),
        _product(),
    ]
    report = validate(items, "Product@1")
    assert report.ok is False
    # e.g. "schema: 2/5 items invalid"
    assert "2/5" in report.reason


def test_required_field_below_fill_floor_fails():
    # price is required; present-but-None (raw) in 2 of 3 -> fill_rate 0.33 < 0.8.
    items = [
        _product(name="A", url="https://shop.example.com/a", price=None),
        _product(name="B", url="https://shop.example.com/b", price=None),
        _product(name="C", url="https://shop.example.com/c"),
    ]
    report = validate(items, "Product@1")
    assert report.ok is False
    assert report.reason == "fill_rate:price"
    assert abs(report.fill_rates["price"] - (1 / 3)) < 1e-9 or report.fill_rates[
        "price"
    ] == round(1 / 3, 2)


def test_optional_field_below_floor_does_not_gate():
    # image_url is optional; absent everywhere (fill 0.0) must NOT fail the report.
    items = [
        _product(name="A", url="https://shop.example.com/a"),
        _product(name="B", url="https://shop.example.com/b"),
    ]
    report = validate(items, "Product@1")
    assert report.ok is True
    assert report.fill_rates["image_url"] == 0.0


def test_empty_list_is_empty_reason():
    report = validate([], "Product@1")
    assert report.ok is False
    assert report.reason == "empty"
    assert report.item_count == 0


def test_baseline_item_count_floor_fails():
    items = [_product(name=f"P{i}", url=f"https://shop.example.com/{i}") for i in range(3)]
    report = validate(items, "Product@1", baseline=10)
    assert report.ok is False
    assert report.reason.startswith("item_count")


def test_baseline_item_count_floor_passes_when_ratio_met():
    # 3 items, baseline 4 -> 3 >= 0.5*4 == 2.0, count gate passes.
    items = [_product(name=f"P{i}", url=f"https://shop.example.com/{i}") for i in range(3)]
    report = validate(items, "Product@1", baseline=4)
    assert report.ok is True
    assert report.reason == ""


def test_failures_listed_even_when_other_gate_decides():
    # A schema failure exists AND price fill is low; schema is checked, failures
    # populated regardless of which reason ultimately decides.
    items = [
        _product(name="A", url="https://shop.example.com/a", price="bad"),
        _product(name="B", url="https://shop.example.com/b", price=None),
        _product(name="C", url="https://shop.example.com/c", price=None),
    ]
    report = validate(items, "Product@1")
    assert report.ok is False
    # schema is the first hard gate -> reason is schema, but the report still
    # records the schema failures it found.
    assert report.failures
