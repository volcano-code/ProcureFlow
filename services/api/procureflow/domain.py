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
        {"id": "P-01", "text": "Only CNY, complete identical SKU coverage, EA units and identical per-SKU requested quantities can be compared."},
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
    if values.lines is not None:
        return calculate_lines(values)
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
    if request.get("lines") is not None or quote.get("lines") is not None:
        return multi_offer_check(request, quote, confirmed, policy)
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


def calculate_lines(values: QuoteValues) -> dict:
    """Whole-supplier basket: independently round each line; freight once.

    A partial or unknown line can never disappear into an aggregate total.
    Scalar tax/discount/delivery fields are forbidden by the multi-line contract.
    """
    calculations = []
    missing, errors = [], []
    for index, line in enumerate(values.lines):
        # Reuse the established decimal cost contract with explicitly zero line
        # freight. The quote's freight is accounted for exactly once below.
        cost = calculate(QuoteValues(**line.model_dump(), supplier_id=values.supplier_id,
                                     currency=values.currency, shipping_cost=Decimal("0")))
        calculations.append({"index": index, "sku": line.sku, **cost})
        missing.extend(f"lines.{index}.{field}" for field in cost["missing"])
        errors.extend(f"lines.{index}.{error}" for error in cost["errors"])
    if values.shipping_cost is None:
        missing.append("shipping_cost")
    comparable = not missing and not errors
    result = {"comparable": comparable, "total": None, "missing": missing, "errors": errors,
              "lines": calculations, "currency": values.currency,
              "shipping": money(values.shipping_cost) if values.shipping_cost is not None else None,
              "rounding": "ROUND_HALF_UP per line: goods and added tax separately; quote freight once"}
    for key in ("goods", "discount", "added_tax"):
        result[key] = money(sum((Decimal(line[key]) for line in calculations), Decimal("0"))) if all(
            line["comparable"] for line in calculations) else None
    if comparable:
        result["total"] = money(sum((Decimal(line["total"]) for line in calculations), Decimal("0")) + values.shipping_cost)
    return result


def request_lines(request: dict) -> list[dict]:
    """Legacy requests stay byte-compatible; normalize only inside comparison."""
    return request["lines"] if request.get("lines") is not None else [
        {key: request[key] for key in ("sku", "quantity", "uom")}]


def multi_offer_check(request: dict, quote: dict, confirmed: bool, policy: dict | None = None) -> dict:
    policy = policy or {}
    values = QuoteValues.model_validate(quote)
    # A legacy scalar quote can be displayed as partial coverage of a basket.
    if values.lines is None:
        from .contracts import QuoteLineValues
        data = values.model_dump()
        values = QuoteValues(supplier_id=values.supplier_id, currency=values.currency,
            shipping_cost=values.shipping_cost,
            lines=[QuoteLineValues(**{key: data[key] for key in QuoteLineValues.model_fields})])
    result = calculate_lines(values)
    budget_limit = min(Decimal(request["budget"]), Decimal(policy["budget_cap"])) if policy.get("budget_cap") is not None else Decimal(request["budget"])
    delivery_limit = min(request["max_delivery_days"], policy["max_delivery_days"]) if policy.get("max_delivery_days") is not None else request["max_delivery_days"]
    wanted = {line["sku"]: line for line in request_lines(request)}
    quoted = {line.sku for line in values.lines if line.sku is not None}
    missing_skus, unexpected = sorted(set(wanted) - quoted), sorted(quoted - set(wanted))
    violations = list(result["errors"])
    if not confirmed:
        violations.append("FIELDS_NOT_CONFIRMED")
    if values.supplier_id is None:
        violations.append("MISSING_SUPPLIER_ID")
    if missing_skus:
        violations.append("MISSING_REQUEST_LINES")
    if unexpected:
        violations.append("UNEXPECTED_QUOTE_LINES")
    for index, line in enumerate(values.lines):
        issues = list(result["lines"][index]["errors"])
        for field in ("sku", "uom", "delivery_days"):
            if getattr(line, field) is None:
                issues.append("MISSING_" + field.upper())
        expected = wanted.get(line.sku)
        if expected is None:
            issues.append("SKU_MISMATCH")
        else:
            if line.quantity != Decimal(expected["quantity"]):
                issues.append("QUANTITY_MISMATCH")
            if line.uom != expected["uom"]:
                issues.append("UOM_MISMATCH")
        if line.delivery_days is not None and line.delivery_days > delivery_limit:
            issues.append("DELIVERY_EXCEEDS_LIMIT")
        issues.extend("UNKNOWN_" + key.upper() for key in result["lines"][index]["missing"])
        result["lines"][index].update(violations=sorted(set(issues)),
            eligible=confirmed and result["lines"][index]["comparable"] and not issues)
        violations.extend(f"LINE_{index + 1}_{issue}" for issue in issues)
    if result["total"] is not None and Decimal(result["total"]) > budget_limit:
        violations.append("BUDGET_EXCEEDED")
    violations.extend("UNKNOWN_" + key.upper() for key in result["missing"])
    return {**result, "violations": sorted(set(violations)), "eligible": result["comparable"] and not violations,
            "coverage": {"requested_skus": list(wanted), "quoted_skus": sorted(quoted),
                         "missing_skus": missing_skus, "unexpected_skus": unexpected,
                         "complete": not missing_skus and not unexpected and len(values.lines) == len(wanted)},
            "effective_limits": {"budget": money(budget_limit), "max_delivery_days": delivery_limit}}
