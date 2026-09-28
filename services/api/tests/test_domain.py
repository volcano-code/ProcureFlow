from decimal import Decimal
import pytest
from pydantic import ValidationError
from procureflow.contracts import QuoteValues, RequestCreate
from procureflow.domain import calculate, digest


def values(**override):
    data=dict(supplier_id="SUP-A",sku="STAND-01",quantity="20",uom="EA",unit_price="1200.00",
              tax_mode="included",tax_rate="0.13",shipping_cost="800.00",discount="0.00",delivery_days=7,currency="CNY")
    return QuoteValues.model_validate({**data, **override})


def test_inclusive_total():
    assert calculate(values())["total"] == "24800.00"


def test_exclusive_total():
    assert calculate(values(tax_mode="excluded"))["total"] == "27920.00"


def test_explicit_discount_before_tax():
    assert calculate(values(quantity="2",unit_price="100.00",tax_mode="excluded",tax_rate="0.10",shipping_cost="5.00",discount="20.00"))["total"] == "203.00"


def test_round_half_up():
    assert calculate(values(quantity="3",unit_price="0.05",tax_mode="excluded",tax_rate="0.1",shipping_cost="0.00"))["total"] == "0.17"


@pytest.mark.parametrize("key", ["shipping_cost", "discount", "unit_price", "quantity", "currency"])
def test_unknown_does_not_become_zero(key):
    result = calculate(values(**{key: None}))
    assert result["total"] is None and key in result["missing"]


def test_unknown_tax():
    assert calculate(values(tax_mode="unknown"))["total"] is None


def test_tax_rate_required_only_if_excluded():
    assert calculate(values(tax_mode="excluded",tax_rate=None))["total"] is None
    assert calculate(values(tax_mode="included",tax_rate=None))["total"] == "24800.00"


@pytest.mark.parametrize("bad", [1.2, -1.0, True, "NaN", "Infinity", "-1", "1.001", "not-a-number"])
def test_money_input_rejects_unsafe_values(bad):
    with pytest.raises((ValidationError, ArithmeticError)):
        values(unit_price=bad)


def test_discount_too_large():
    assert "DISCOUNT_EXCEEDS_GOODS" in calculate(values(discount="24001.00"))["errors"]


def test_currency_not_converted():
    assert calculate(values(currency="USD"))["comparable"] is False


def test_hash_dict_order_invariant():
    assert digest({"b":2,"a":Decimal("1.00")}) == digest({"a":Decimal("1"),"b":2})


def test_quantity_json_number_rejected():
    with pytest.raises(ValidationError):
        RequestCreate(title="x",sku="x",quantity=20,budget="20.00")
