import pytest
from decimal import Decimal
from crawloop.schemas import get_schema, SchemaNotFound


def test_get_schema_by_versioned_ref():
    model = get_schema("Product@1")
    obj = model(name="A", price=Decimal("9.99"), in_stock=True, url="https://x.com/a")
    assert obj.price == Decimal("9.99")


def test_volatile_fields_exposed():
    assert "price" in get_schema("Product@1").VOLATILE


def test_unknown_ref_raises():
    with pytest.raises(SchemaNotFound):
        get_schema("Nope@1")


def test_extra_fields_forbidden():
    model = get_schema("Product@1")
    with pytest.raises(Exception):
        model(name="A", price=Decimal("1"), in_stock=True, url="https://x.com/a", junk=1)


def test_schema_json_dump():
    from crawloop.schemas import schema_json
    js = schema_json("Product@1")
    assert "price" in js["properties"]
