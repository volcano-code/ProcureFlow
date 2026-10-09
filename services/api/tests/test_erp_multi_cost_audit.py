"""Independent DB evidence: no adapter mapping or POST echo builds this fixture."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "integrations/erpnext/sandbox"))
from cost_fixtures import FREIGHT_ACCOUNT, MULTI_ITEM_CASE
from multi_cost_audit import verify_multi_cost_document


def persisted_document():
    """Literal expected database state, independently specified from the adapter."""
    return {
        "doctype": "Supplier Quotation", "company": "ProcureFlow Sandbox", "currency": "CNY",
        "docstatus": 0, "total": "41.80", "net_total": "41.80", "grand_total": "47.05",
        "discount_amount": "0", "additional_discount_percentage": "0", "conversion_rate": "1",
        "disable_rounded_total": 1, "apply_discount_on": "Grand Total", "total_taxes_and_charges": "5.25",
        "taxes_and_charges": "", "shipping_rule": "", "tax_category": "", "pricing_rules": [],
        "items": [
            {
                "item_code": "PF-SANDBOX-ITEM", "uom": "EA", "qty": "2", "rate": "10.25",
                "amount": "20.50", "net_amount": "20.50", "net_rate": "10.25", "price_list_rate": "10.25",
                "discount_amount": "0", "discount_percentage": "0", "item_tax_template": "", "pricing_rules": "",
                "description": 'ProcureFlow line terms: {"delivery_days":3,"discount":"0","tax_mode":"included","tax_rate":"0"}',
            },
            {
                "item_code": "PF-SANDBOX-ITEM-2", "uom": "EA", "qty": "3", "rate": "7.10",
                "amount": "21.30", "net_amount": "21.30", "net_rate": "7.10", "price_list_rate": "7.10",
                "discount_amount": "0", "discount_percentage": "0", "item_tax_template": "", "pricing_rules": "",
                "description": 'ProcureFlow line terms: {"delivery_days":4,"discount":"0","tax_mode":"included","tax_rate":"0"}',
            },
        ],
        "taxes": [{
            "category": "Total", "add_deduct_tax": "Add", "charge_type": "Actual", "account_head": FREIGHT_ACCOUNT,
            "description": "ProcureFlow gross freight", "rate": None, "included_in_print_rate": 0,
            "dont_recompute_tax": 0, "row_id": "", "tax_amount": "5.25",
            "tax_amount_after_discount_amount": "5.25", "total": "47.05",
        }],
    }


def parent_and_key(record, path):
    for key in path[:-1]:
        record = record[key]
    return record, path[-1]


HEADER_NUMBERS = (
    "total", "net_total", "grand_total", "discount_amount", "additional_discount_percentage",
    "conversion_rate", "total_taxes_and_charges",
)
ITEM_NUMBERS = ("qty", "rate", "amount", "net_amount", "net_rate", "price_list_rate", "discount_amount", "discount_percentage")
FREIGHT_NUMBERS = ("rate", "tax_amount", "tax_amount_after_discount_amount", "total")
NUMERIC_PATHS = ([(field,) for field in HEADER_NUMBERS]
                 + [("items", index, field) for index in (0, 1) for field in ITEM_NUMBERS]
                 + [("taxes", 0, field) for field in FREIGHT_NUMBERS])
FLAG_PATHS = [("docstatus",), ("disable_rounded_total",),
              ("taxes", 0, "included_in_print_rate"), ("taxes", 0, "dont_recompute_tax")]
TEXT_PATHS = [("company",), ("currency",), ("apply_discount_on",)] + [
    ("items", index, field) for index in (0, 1) for field in ("item_code", "uom", "description")
] + [("taxes", 0, field) for field in ("category", "add_deduct_tax", "charge_type", "account_head", "description")]
BLANK_TEXT_PATHS = [(field,) for field in ("taxes_and_charges", "shipping_rule", "tax_category")] + [
    ("items", index, field) for index in (0, 1) for field in ("item_tax_template", "pricing_rules")
] + [("taxes", 0, "row_id")]


def test_independent_two_item_database_fixture_matches_without_mutation():
    record = persisted_document()
    original, expected = deepcopy(record), deepcopy(MULTI_ITEM_CASE)
    verify_multi_cost_document(record, expected)
    assert record == original
    assert expected == MULTI_ITEM_CASE


def test_complete_item_identity_set_is_order_independent():
    record = persisted_document()
    record["items"].reverse()
    verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("numeric_type", [str, int, float, Decimal])
def test_exact_database_numeric_types_are_supported(numeric_type):
    record = persisted_document()
    for path in NUMERIC_PATHS:
        parent, key = parent_and_key(record, path)
        value = Decimal("0" if parent[key] is None else parent[key])
        if numeric_type is not int or value == value.to_integral_value():
            parent[key] = numeric_type(value)
    verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", NUMERIC_PATHS + FLAG_PATHS + TEXT_PATHS + [("items",), ("taxes",)])
def test_missing_required_database_fields_fail_closed(path):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    del parent[key]
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", NUMERIC_PATHS)
def test_each_numeric_component_is_audited_even_with_unchanged_grand_total(path):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = str(Decimal("0" if parent[key] is None else parent[key]) + Decimal("0.01"))
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", NUMERIC_PATHS)
@pytest.mark.parametrize("value", [False, True, "NaN", "sNaN", "Infinity", "-Infinity", float("nan"), Decimal("Infinity"), "", [], {}])
def test_malformed_booleans_and_nonfinite_numbers_are_never_cost_evidence(path, value):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", [path for path in NUMERIC_PATHS if path != ("taxes", 0, "rate")])
def test_null_required_cost_numbers_fail_closed(path):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = None
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", FLAG_PATHS)
@pytest.mark.parametrize("value", [False, True, None, "0", "1", 0.0, 1.0, 2, -1, [], {}])
def test_database_flags_require_exact_integer_controls(path, value):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", TEXT_PATHS)
@pytest.mark.parametrize("value", [None, False, 0, "", "changed", [], {}])
def test_all_line_identities_descriptions_and_header_freight_controls_match(path, value):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path", BLANK_TEXT_PATHS)
@pytest.mark.parametrize("value", [False, 0, [], {}, "unexpected rule", " "])
def test_templates_pricing_and_dependent_tax_controls_reject_malformed_falsy_values(path, value):
    record = persisted_document()
    parent, key = parent_and_key(record, path)
    parent[key] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("value", [None, ""])
def test_nullable_erp_text_controls_are_supported(value):
    record = persisted_document()
    for path in BLANK_TEXT_PATHS + [("pricing_rules",)]:
        parent, key = parent_and_key(record, path)
        parent[key] = value
    verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("value", [False, 0, {}, ["unexpected rule"], "unexpected rule"])
def test_header_pricing_rules_require_an_empty_table_or_nullable_text(value):
    record = persisted_document()
    record["pricing_rules"] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("table", ["items", "taxes"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "null", "mapping", "non_record"])
def test_missing_extra_or_malformed_rows_fail_closed(table, mutation):
    record = persisted_document()
    if mutation == "missing":
        record[table].pop()
    elif mutation == "extra":
        record[table].append(deepcopy(record[table][0]))
    elif mutation == "null":
        record[table] = None
    elif mutation == "mapping":
        record[table] = {}
    else:
        record[table][0] = None
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


def test_duplicate_sku_cannot_hide_a_missing_line():
    record = persisted_document()
    record["items"][1]["item_code"] = record["items"][0]["item_code"]
    with pytest.raises(AssertionError, match="identity_set"):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("field", ["qty", "rate", "amount", "net_amount", "net_rate", "price_list_rate", "description"])
def test_swapped_per_sku_components_cannot_hide_behind_matching_totals(field):
    record = persisted_document()
    first, second = record["items"]
    first[field], second[field] = second[field], first[field]
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("field", ["amount", "net_amount"])
def test_compensating_line_amounts_still_fail(field):
    record = persisted_document()
    record["items"][0][field] = "20.51"
    record["items"][1][field] = "21.29"
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


def test_compensating_freight_and_goods_still_fail():
    record = persisted_document()
    record.update(total="40.80", net_total="40.80", total_taxes_and_charges="6.25")
    record["items"][0].update(amount="19.50", net_amount="19.50")
    record["taxes"][0].update(tax_amount="6.25", tax_amount_after_discount_amount="6.25")
    assert record["grand_total"] == "47.05"
    with pytest.raises(AssertionError):
        verify_multi_cost_document(record, MULTI_ITEM_CASE)


def test_audit_checks_remain_active_under_python_optimization():
    path = ROOT / "integrations/erpnext/sandbox/multi_cost_audit.py"
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec", optimize=2), namespace)
    record = persisted_document()
    record["items"][1]["net_amount"] = "21.31"
    with pytest.raises(AssertionError):
        namespace["verify_multi_cost_document"](record, MULTI_ITEM_CASE)


@pytest.mark.parametrize("path,value", [
    (("tax_mode",), "excluded"), (("tax_rate",), "0.13"), (("discount",), "1"),
    (("tax_before",), "1"), (("tax_after",), "1"), (("shipping_cost",), "0"),
    (("goods",), "1"), (("net",), "1"), (("total",), "1"),
    (("lines", 0, "tax_mode"), "excluded"), (("lines", 1, "tax_rate"), "0.13"),
    (("lines", 1, "discount"), "1"), (("lines", 0, "quantity"), "2.5"),
    (("lines", 0, "unit_price"), "Infinity"), (("lines", 0, "delivery_days"), True),
    (("lines", 0, "uom"), "BOX"), (("lines", 1, "sku"), "PF-SANDBOX-ITEM"),
])
def test_fixture_must_remain_inside_the_explicit_supported_contract(path, value):
    expected = deepcopy(MULTI_ITEM_CASE)
    parent, key = parent_and_key(expected, path)
    parent[key] = value
    with pytest.raises(AssertionError):
        verify_multi_cost_document(persisted_document(), expected)
