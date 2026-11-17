"""Independent database audit for the synthetic, two-item ERP cost contract.

This module deliberately imports neither the application nor its cost mapper.
The caller obtains the document from the ERP database and verifies the operation,
snapshot, and supplier identities separately. A POST response is not evidence.
"""

import json
from decimal import Decimal, InvalidOperation

from cost_fixtures import FREIGHT_ACCOUNT


COMPANY = "ProcureFlow Sandbox"
FREIGHT_DESCRIPTION = "ProcureFlow gross freight"


def _require(condition, field):
    # Keep the audit active even when its caller runs Python with optimization.
    if not condition:
        raise AssertionError(field)


def _number(value, field):
    _require(type(value) in (str, int, float, Decimal), field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AssertionError(field) from None
    _require(result.is_finite(), field)
    return result


def _numeric_fields(record, expected, prefix):
    for field, wanted in expected.items():
        _require(field in record, f"{prefix}.{field}")
        _require(_number(record[field], f"{prefix}.{field}")
                 == _number(wanted, f"expected.{field}"), f"{prefix}.{field}")


def _flag(record, field, wanted, prefix):
    _require(type(record.get(field)) is int and record[field] == wanted, f"{prefix}.{field}")


def _blank(value):
    """Only ERP nullable text is blank; False, zero, lists, and dicts are not."""
    return value is None or (type(value) is str and value == "")


def verify_multi_cost_document(doc, expected):
    """Fail closed on any persisted component drift, including offsetting drift.

The independently specified fixture is intentionally limited to two unique EA
items, explicit included zero tax, zero discounts, and positive gross freight.
Item order is irrelevant, but the complete identity set and every line's terms
must match. ERP nullable template fields may be absent, null, or empty text;
header pricing_rules additionally permits an empty ERP child table.
"""
    _require(isinstance(doc, dict) and isinstance(expected, dict), "document/expected")
    lines = expected.get("lines")
    _require(isinstance(lines, list) and len(lines) == 2, "expected.lines")
    _require(all(isinstance(line, dict) and type(line.get("sku")) is str
                 and line["sku"] for line in lines), "expected.lines.sku")
    _require(len({line["sku"] for line in lines}) == 2, "expected.lines.duplicate_sku")
    _require(expected.get("tax_mode") == "included", "expected.tax_mode")
    for field in ("tax_rate", "discount", "tax_before", "tax_after"):
        _require(_number(expected.get(field), f"expected.{field}") == 0, f"expected.{field}")
    for line in lines:
        _require(line.get("tax_mode") == "included" and line.get("uom") == "EA", "expected.line.terms")
        for field in ("tax_rate", "discount"):
            _require(_number(line.get(field), f"expected.line.{field}") == 0, f"expected.line.{field}")
        _require(type(line.get("delivery_days")) is int and line["delivery_days"] >= 0,
                 "expected.line.delivery_days")
        quantity = _number(line.get("quantity"), "expected.line.quantity")
        price = _number(line.get("unit_price"), "expected.line.unit_price")
        _require(quantity > 0 and quantity == quantity.to_integral_value() and price >= 0,
                 "expected.line.quantity/unit_price")
        goods = _number(line.get("goods"), "expected.line.goods")
        _require(goods == quantity * price == _number(line.get("net"), "expected.line.net"),
                 "expected.line.goods/net")
    goods = sum((_number(line["goods"], "expected.line.goods") for line in lines), Decimal(0))
    freight_amount = _number(expected.get("shipping_cost"), "expected.shipping_cost")
    _require(freight_amount > 0, "expected.shipping_cost")
    _require(goods == _number(expected.get("goods"), "expected.goods")
             == _number(expected.get("net"), "expected.net"), "expected.goods/net")
    _require(goods + freight_amount == _number(expected.get("total"), "expected.total"), "expected.total")

    _require(doc.get("currency") == "CNY" and doc.get("company") == COMPANY, "document.currency/company")
    _flag(doc, "docstatus", 0, "document")
    _flag(doc, "disable_rounded_total", 1, "document")
    _require(doc.get("apply_discount_on") == "Grand Total", "document.apply_discount_on")
    _numeric_fields(doc, {
        "total": expected["goods"], "net_total": expected["net"], "grand_total": expected["total"],
        "discount_amount": "0", "additional_discount_percentage": "0", "conversion_rate": "1",
        "total_taxes_and_charges": expected["shipping_cost"],
    }, "document")
    for field in ("taxes_and_charges", "shipping_rule", "tax_category"):
        _require(_blank(doc.get(field)), f"document.{field}")
    pricing_rules = doc.get("pricing_rules")
    _require(_blank(pricing_rules) or (type(pricing_rules) is list and len(pricing_rules) == 0),
             "document.pricing_rules")

    items = doc.get("items")
    _require(isinstance(items, list) and len(items) == 2, "document.items")
    _require(all(isinstance(item, dict) and type(item.get("item_code")) is str
                 for item in items), "document.items.item_code")
    actual_skus = [item["item_code"] for item in items]
    _require(len(set(actual_skus)) == 2 and set(actual_skus) == {line["sku"] for line in lines},
             "document.items.identity_set")
    by_sku = {item["item_code"]: item for item in items}
    for line in lines:
        item, prefix = by_sku[line["sku"]], f"item[{line['sku']}]"
        _require(item.get("uom") == "EA", f"{prefix}.uom")
        description = "ProcureFlow line terms: " + json.dumps(
            {field: line[field] for field in ("tax_mode", "tax_rate", "discount", "delivery_days")},
            sort_keys=True, separators=(",", ":"))
        _require(item.get("description") == description, f"{prefix}.description")
        _numeric_fields(item, {
            "qty": line["quantity"], "rate": line["unit_price"], "price_list_rate": line["unit_price"],
            "net_rate": line["unit_price"], "amount": line["goods"], "net_amount": line["net"],
            "discount_percentage": "0", "discount_amount": "0",
        }, prefix)
        for field in ("item_tax_template", "pricing_rules"):
            _require(_blank(item.get(field)), f"{prefix}.{field}")

    taxes = doc.get("taxes")
    _require(isinstance(taxes, list) and len(taxes) == 1 and isinstance(taxes[0], dict), "document.taxes")
    freight = taxes[0]
    for field, wanted in {
        "category": "Total", "add_deduct_tax": "Add", "charge_type": "Actual",
        "account_head": FREIGHT_ACCOUNT, "description": FREIGHT_DESCRIPTION,
    }.items():
        _require(freight.get(field) == wanted, f"freight.{field}")
    _flag(freight, "included_in_print_rate", 0, "freight")
    _flag(freight, "dont_recompute_tax", 0, "freight")
    _require(_blank(freight.get("row_id")), "freight.row_id")
    _require("rate" in freight, "freight.rate")
    if freight["rate"] is not None:
        _require(_number(freight["rate"], "freight.rate") == 0, "freight.rate")
    _numeric_fields(freight, {
        "tax_amount": expected["shipping_cost"],
        "tax_amount_after_discount_amount": expected["shipping_cost"], "total": expected["total"],
    }, "freight")
