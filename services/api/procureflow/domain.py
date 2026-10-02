from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from .contracts import QuoteValues

DEMO_POLICY = {
    "id": "demo-procurement-policy",
    "version": "1",
    "published": True,
    "scope": "synthetic local demonstration, not a real company policy",
    "clauses": [
        {"id": "P-01", "text": "Only CNY, identical SKU, EA unit and identical requested quantity can be compared."},
        {"id": "P-02", "text": "Shipping and discounts must be explicit; unknown is not zero. Tax status must be known."},
        {"id": "P-03", "text": "The total must not exceed budget and delivery must meet the requested deadline."},
        {"id": "P-04", "text": "A different authorized approver must approve an unchanged snapshot before a draft write."},
    ],
}


# Legacy fixture export only; runtime policy is persisted per tenant.
POLICY = DEMO_POLICY

def canonical(value) -> str:
    """Domain-normalized decimal strings; no float serialization in monetary calculations."""
    def normalize(obj):
        if isinstance(obj, Decimal):
            return format(obj.normalize(), "f")
        if isinstance(obj, dict):
            return {k: normalize(v) for k, v in obj.items()}
        if isinstance(obj, (tuple, list)):
            return [normalize(v) for v in obj]
        return obj
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def calculate(values: QuoteValues) -> dict:
    """Single SKU; absolute goods discount; freight is explicitly tax-inclusive.

    Round goods to cents, apply absolute discount, apply tax if excluded, then
    add the final gross freight charge. No exchange-rate/UOM inference.
    """
    required = ["quantity", "unit_price", "shipping_cost", "discount", "currency"]
    missing = [key for key in required if getattr(values, key) is None]
    if values.tax_mode == "unknown":
        missing.append("tax_mode")
    if values.tax_mode == "excluded" and values.tax_rate is None:
        missing.append("tax_rate")
    if missing:
        return {"comparable": False, "total": None, "missing": missing, "errors": []}
    if values.currency != "CNY":
        return {"comparable": False, "total": None, "missing": [], "errors": ["UNSUPPORTED_CURRENCY"]}
    gross_or_net = Decimal(money(values.quantity * values.unit_price))
    if values.discount > gross_or_net:
        return {"comparable": False, "total": None, "missing": [], "errors": ["DISCOUNT_EXCEEDS_GOODS"]}
    discounted = gross_or_net - values.discount
    vat = Decimal("0") if values.tax_mode == "included" else Decimal(money(discounted * values.tax_rate))
    total = discounted + vat + values.shipping_cost
    return {
        "comparable": True, "total": money(total), "missing": [], "errors": [],
        "goods": money(gross_or_net), "discount": money(values.discount),
        "added_tax": money(vat), "shipping": money(values.shipping_cost),
        "currency": "CNY", "rounding": "ROUND_HALF_UP, goods and added tax rounded separately",
    }


def offer_check(request: dict, quote: dict, confirmed: bool, policy: dict | None = None) -> dict:
    policy = policy or {}
    budget_limit = min(Decimal(request["budget"]), Decimal(policy["budget_cap"])) if policy.get("budget_cap") is not None else Decimal(request["budget"])
    delivery_limit = min(request["max_delivery_days"], policy["max_delivery_days"]) if policy.get("max_delivery_days") is not None else request["max_delivery_days"]
    values = QuoteValues.model_validate(quote)
    result = calculate(values)
    violations = list(result["errors"])
    if not confirmed:
        violations.append("FIELDS_NOT_CONFIRMED")
    for field in ("supplier_id", "sku", "uom", "delivery_days"):
        if getattr(values, field) is None:
            violations.append(f"MISSING_{field.upper()}")
    if values.sku != request["sku"]:
        violations.append("SKU_MISMATCH")
    if values.uom != request["uom"]:
        violations.append("UOM_MISMATCH")
    if values.quantity != Decimal(request["quantity"]):
        violations.append("QUANTITY_MISMATCH")
    if values.delivery_days is not None and values.delivery_days > delivery_limit:
        violations.append("DELIVERY_EXCEEDS_LIMIT")
    if result["total"] is not None and Decimal(result["total"]) > budget_limit:
        violations.append("BUDGET_EXCEEDED")
    if result["missing"]:
        violations.extend("UNKNOWN_" + key.upper() for key in result["missing"])
    return {**result, "violations": sorted(set(violations)), "eligible": result["comparable"] and not violations,
            "effective_limits": {"budget": money(budget_limit), "max_delivery_days": delivery_limit}}
