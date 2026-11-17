from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
import httpx
from .domain import canonical, digest, calculate
from .contracts import QuoteValues
from .outbound import validate_endpoint, request_json, ResponseLimitError, ResponseDeadlineError


class ERPUnknown(Exception):
    """A request may have committed remotely. Do not blindly retry a write."""


class ERPRejected(Exception):
    """Configuration or explicit business rejection. Human review is required."""


class ERPPort(Protocol):
    mode: str
    def suppliers(self) -> list[dict]: ...
    def find(self, operation_key: str) -> dict | None: ...
    def create_draft(self, operation_key, payload: dict) -> dict: ...
    def create_draft_guarded(self, operation_key, payload: dict, before_write) -> dict: ...


class MockERP:
    """An explicitly simulated ERP persisted in a separate SQLite database.

    A unique remote key and payload hash implement atomic insert-or-return.
    This is NOT a live ERPNext instance or evidence of live ERP reliability.
    """
    mode = "mock"

    def __init__(self, path: Path):
        self.path = path
        self.fail_after_commit_once: set[str] = set()
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS drafts (operation_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, data TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=20)

    def suppliers(self):
        return [{"name": key, "supplier_name": name} for key, name in [
            ("SUP-A", "青岚办公（合成样例）"), ("SUP-B", "远川设备（合成样例）"), ("SUP-C", "北辰供应（合成样例）")]]

    def find(self, operation_key):
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM drafts WHERE operation_key=?", (operation_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def count(self):
        with self._connect() as connection:
            return connection.execute("SELECT count(*) FROM drafts").fetchone()[0]

    def create_draft_guarded(self, operation_key, payload, before_write):
        before_write()
        return self.create_draft(operation_key, payload)

    def create_draft(self, operation_key, payload):
        v = payload["quote_values"]
        if v["supplier_id"] not in {s["name"] for s in self.suppliers()}:
            raise ERPRejected("UNKNOWN_SUPPLIER")
        record = {"name": "MOCK-SQ-" + operation_key[:12].upper(), "docstatus": 0,
                  "snapshot_hash": payload["snapshot_hash"], "operation_key": operation_key, "supplier_id": v["supplier_id"],
                  "sku": v.get("sku"), "quantity": v.get("quantity"), "unit_price": v.get("unit_price"),
                  "transaction_date": payload["transaction_date"], "total": payload["total"],
                  "currency": v["currency"], "uom": v.get("uom"), "company": payload["erp_company"], "simulated": True,
                  "cost_values": expected_mock_cost_values(payload)}
        if v.get("lines") is not None:
            record["lines"] = quote_item_values(payload)
        payload_hash = digest(payload)
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO drafts(operation_key,payload_hash,data) VALUES (?,?,?)",
                               (operation_key, payload_hash, canonical(record)))
            row = connection.execute("SELECT payload_hash,data FROM drafts WHERE operation_key=?", (operation_key,)).fetchone()
            if row[0] != payload_hash:
                raise ERPRejected("IDEMPOTENCY_PAYLOAD_CONFLICT")
        if operation_key in self.fail_after_commit_once:
            self.fail_after_commit_once.remove(operation_key)
            raise ERPUnknown("SIMULATED_RESPONSE_LOST_AFTER_COMMIT")
        return json.loads(row[1])


COST_MAPPING_VERSION = "single-line-costs-v1"
MULTI_COST_MAPPING_VERSION = "multi-line-zero-tax-costs-v1"
MAX_ERP_LINES = 20
MAX_ERP_AMOUNT = Decimal("1000000.00")
TAX_DESCRIPTION = "ProcureFlow goods tax"
FREIGHT_DESCRIPTION = "ProcureFlow gross freight"


def optional_text(value):
    """ERP nullable text is blank; other falsy JSON types are malformed."""
    return "" if value is None or value == "" else value


def cost_mapping(payload: dict) -> tuple[dict, dict]:
    """Explicit CNY/EA commercial totals, not a statutory/accounting tax engine.

    ERPNext v16.36.0: inclusive goods tax is On Net Total/included_in_print_rate;
    Actual freight is excluded from Grand Total discount distribution. Reject
    inclusive-discount penny redistribution we cannot represent exactly.
    """
    try:
        v = QuoteValues.model_validate(payload["quote_values"])
        if v.lines is not None:
            return multi_cost_mapping(payload, v)
        calculated = calculate(v)
        if (not calculated["comparable"] or v.tax_rate is None or v.uom != "EA"
                or not v.supplier_id or not v.sku):
            raise ERPRejected("ERP_COST_MAPPING_UNSUPPORTED")
        if (v.quantity != v.quantity.to_integral_value()
                or any(Decimal(calculated[key]) > MAX_ERP_AMOUNT for key in ('goods', 'total'))
                or any(amount > MAX_ERP_AMOUNT for amount in (v.unit_price, v.shipping_cost, v.discount))):
            raise ERPRejected("ERP_COST_MAPPING_BOUNDS_EXCEEDED")
        if Decimal(str(payload["total"])) != Decimal(calculated["total"]):
            raise ERPRejected("ERP_PAYLOAD_TOTAL_MISMATCH")
        accounts = payload.get("erp_cost_accounts", {})
        tax_account, freight_account = accounts.get("tax", ""), accounts.get("freight", "")
        for needed, account in ((v.tax_rate > 0, tax_account), (v.shipping_cost > 0, freight_account)):
            if needed and (not isinstance(account, str) or not account.strip() or len(account) > 140):
                raise ERPRejected("ERP_COST_ACCOUNT_REQUIRED")
        if v.tax_rate > 0 and v.shipping_cost > 0 and tax_account == freight_account:
            raise ERPRejected("ERP_COST_ACCOUNTS_MUST_DIFFER")
        cents = lambda n: n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        goods = Decimal(calculated["goods"])
        discounted = goods - v.discount
        inclusive = v.tax_mode == "included"
        net = goods
        if inclusive and v.tax_rate:
            net = cents(goods / (1 + v.tax_rate))
            before_tax = cents(goods / (1 + v.tax_rate) * v.tax_rate)
            if net + before_tax != goods:
                raise ERPRejected("ERP_INCLUSIVE_ROUNDING_UNSUPPORTED")
            if v.discount:
                net = cents(net * discounted / goods)
                after_tax = cents(net * v.tax_rate)
            else:
                after_tax = before_tax
            # Do not silently use ERP's inclusive rounding adjustment as proof.
            if net + after_tax != discounted:
                raise ERPRejected("ERP_INCLUSIVE_ROUNDING_UNSUPPORTED")
        else:
            net = discounted
            before_tax = after_tax = cents(net * v.tax_rate)
        rows = []
        def row(account, description, charge_type, rate, amount, included):
            return {"category": "Total", "add_deduct_tax": "Add", "charge_type": charge_type,
                    "account_head": account, "description": description, "rate": str(rate),
                    "tax_amount": str(amount), "included_in_print_rate": included,
                    "row_id": "", "dont_recompute_tax": 0}
        if v.tax_rate:
            rows.append(row(tax_account, TAX_DESCRIPTION, "On Net Total", v.tax_rate * 100, 0, int(inclusive)))
        if v.shipping_cost:
            rows.append(row(freight_account, FREIGHT_DESCRIPTION, "Actual", 0, v.shipping_cost, 0))
        body = {"taxes": rows, "apply_discount_on": "Grand Total" if inclusive else "Net Total",
                "discount_amount": str(v.discount), "additional_discount_percentage": "0",
                "disable_rounded_total": 1, "conversion_rate": "1",
                "taxes_and_charges": "", "shipping_rule": "", "tax_category": ""}
        expected_rows = []
        for entry in rows:
            tax = entry["description"] == TAX_DESCRIPTION
            expected_rows.append({**entry, "tax_amount": str(before_tax if tax else v.shipping_cost),
                "tax_amount_after_discount_amount": str(after_tax if tax else v.shipping_cost),
                "total": str(net + after_tax + (0 if tax else v.shipping_cost))})
        proof = {"mapping_version": COST_MAPPING_VERSION, "taxes": expected_rows,
            "apply_discount_on": body["apply_discount_on"], "discount_amount": str(v.discount),
            "additional_discount_percentage": "0", "net_total": str(net),
            "total_taxes_and_charges": str(after_tax + v.shipping_cost),
            "taxes_and_charges": "", "shipping_rule": "", "tax_category": "", "item_tax_template": "",
            "pricing_rules": "", "item_pricing_rules": "",
            "item_discount_amount": "0", "item_discount_percentage": "0", "price_list_rate": str(v.unit_price),
            "item_net_rate": str(cents(net / v.quantity)),
            "goods_total": str(goods), "item_goods_total": str(goods), "item_net_total": str(net), "conversion_rate": "1", "disable_rounded_total": 1}
        return body, proof
    except ERPRejected:
        raise
    except (ValueError, TypeError, ArithmeticError, KeyError, AttributeError) as error:
        raise ERPRejected("ERP_PAYLOAD_INVALID") from error


def quote_item_values(payload: dict) -> list[dict]:
    """A complete bounded identity set, in case-sensitive SKU order."""
    values = payload["quote_values"]
    rows = values.get("lines")
    if (not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ERP_LINES
            or any(not isinstance(row, dict) or not isinstance(row.get("sku"), str)
                   or not row["sku"] for row in rows)
            or len({row["sku"] for row in rows}) != len(rows)):
        raise ERPRejected("ERP_ITEM_SET_INVALID")
    return [{key: row.get(key) for key in ("sku", "quantity", "uom", "unit_price")}
            for row in sorted(rows, key=lambda row: row["sku"])]


def expected_mock_cost_values(payload: dict) -> dict:
    """Keep every synthetic cost/delivery component, without borrowing real-ERP limits."""
    raw = payload["quote_values"]
    if raw.get("lines") is None:
        return {key: raw.get(key) for key in ("tax_mode", "tax_rate", "shipping_cost", "discount")}
    try:
        values = QuoteValues.model_validate(raw)
        quote_item_values(payload)
        calculated = calculate(values)
        if not calculated["comparable"] or Decimal(str(payload["total"])) != Decimal(calculated["total"]):
            raise ERPRejected("ERP_PAYLOAD_TOTAL_MISMATCH")
        return {"shipping_cost": raw.get("shipping_cost"),
                "lines": [{key: row.get(key) for key in
                           ("sku", "tax_mode", "tax_rate", "discount", "delivery_days")}
                          for row in sorted(raw["lines"], key=lambda row: row["sku"])],
                "calculated": calculated}
    except ERPRejected:
        raise
    except (ValueError, TypeError, ArithmeticError, KeyError, AttributeError) as error:
        raise ERPRejected("ERP_PAYLOAD_INVALID") from error


def multi_line_description(line) -> str:
    """Persist bounded commercial terms in a standard, independently read field."""
    return "ProcureFlow line terms: " + canonical({key: getattr(line, key) for key in
        ("tax_mode", "tax_rate", "discount", "delivery_days")})


def multi_cost_mapping(payload: dict, values: QuoteValues) -> tuple[dict, dict]:
    """Offline-contract-only, bounded zero-tax/zero-discount multi-item mapping.

    Supports CNY/EA integral quantities, one common explicit tax mode and zero
    tax rates/discounts. Freight is one final gross Actual row. Positive/mixed
    goods tax and line discounts need a separately validated mapping; they are
    rejected before any network I/O rather than silently redistributed.
    """
    calculated = calculate(values)
    lines = values.lines
    quote_item_values(payload)
    if (not calculated["comparable"] or not values.supplier_id
            or any(line.uom != "EA" or line.tax_rate is None or line.tax_rate != 0
                   or line.discount != 0 or not line.sku for line in lines)
            or len({line.tax_mode for line in lines}) != 1):
        raise ERPRejected("ERP_MULTI_COST_MAPPING_UNSUPPORTED")
    if (any(line.quantity != line.quantity.to_integral_value()
            or line.unit_price > MAX_ERP_AMOUNT for line in lines)
            or values.shipping_cost > MAX_ERP_AMOUNT
            or any(Decimal(calculated[key]) > MAX_ERP_AMOUNT for key in ("goods", "total"))):
        raise ERPRejected("ERP_COST_MAPPING_BOUNDS_EXCEEDED")
    if Decimal(str(payload["total"])) != Decimal(calculated["total"]):
        raise ERPRejected("ERP_PAYLOAD_TOTAL_MISMATCH")
    freight_account = payload.get("erp_cost_accounts", {}).get("freight", "")
    if values.shipping_cost and (not isinstance(freight_account, str)
            or not freight_account.strip() or len(freight_account) > 140):
        raise ERPRejected("ERP_COST_ACCOUNT_REQUIRED")
    taxes = []
    if values.shipping_cost:
        taxes.append({"category": "Total", "add_deduct_tax": "Add", "charge_type": "Actual",
            "account_head": freight_account, "description": FREIGHT_DESCRIPTION, "rate": "0",
            "tax_amount": str(values.shipping_cost), "included_in_print_rate": 0,
            "row_id": "", "dont_recompute_tax": 0})
    body = {"taxes": taxes, "apply_discount_on": "Grand Total" if lines[0].tax_mode == "included" else "Net Total",
            "discount_amount": "0", "additional_discount_percentage": "0", "disable_rounded_total": 1,
            "conversion_rate": "1", "taxes_and_charges": "", "shipping_rule": "", "tax_category": ""}
    cents = lambda amount: amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    proof = {"mapping_version": MULTI_COST_MAPPING_VERSION,
        "taxes": [{**row, "tax_amount_after_discount_amount": str(values.shipping_cost),
                   "total": calculated["total"]} for row in taxes],
        **{key: value for key, value in body.items() if key != "taxes"},
        "goods_total": calculated["goods"], "net_total": calculated["goods"],
        "total_taxes_and_charges": str(values.shipping_cost), "pricing_rules": "",
        "lines": [{"sku": line.sku, "quantity": str(line.quantity), "uom": line.uom,
                   "unit_price": str(line.unit_price), "line_terms": multi_line_description(line),
                   "item_tax_template": "", "item_pricing_rules": "",
                   "item_discount_amount": "0", "item_discount_percentage": "0",
                   "price_list_rate": str(line.unit_price), "item_net_rate": str(line.unit_price),
                   "item_goods_total": str(cents(line.quantity * line.unit_price)),
                   "item_net_total": str(cents(line.quantity * line.unit_price))}
                  for line in sorted(lines, key=lambda line: line.sku)]}
    return body, proof


def cost_proof_matches(observed, expected):
    """Compare every persisted component, not merely a compensating grand total."""
    numeric = {"rate", "tax_amount", "tax_amount_after_discount_amount", "discount_amount",
               "additional_discount_percentage", "net_total", "total_taxes_and_charges",
               "goods_total", "item_goods_total", "item_net_total", "total", "conversion_rate",
               "item_discount_amount", "item_discount_percentage", "price_list_rate", "item_net_rate",
               "quantity", "unit_price", "tax_rate", "discount", "shipping_cost", "goods", "added_tax", "shipping"}

    def match(actual, wanted, key=None):
        if isinstance(wanted, dict):
            return (isinstance(actual, dict) and set(actual) == set(wanted)
                    and all(match(actual[k], value, k) for k, value in wanted.items()))
        if isinstance(wanted, list):
            return (isinstance(actual, list) and len(actual) == len(wanted)
                    and all(match(a, w) for a, w in zip(actual, wanted, strict=True)))
        if key in numeric and wanted is not None:
            if isinstance(actual, bool):
                return False
            left, right = Decimal(str(actual)), Decimal(str(wanted))
            return left.is_finite() and right.is_finite() and left == right
        return type(actual) is type(wanted) and actual == wanted

    try:
        return isinstance(observed, dict) and isinstance(expected, dict) and match(observed, expected)
    except (ValueError, TypeError, ArithmeticError):
        return False


def erp_numeric_json(body: dict) -> bytes:
    """Encode only the adapter's explicit ERP numeric fields as JSON numbers.

    Our API/snapshot decimal strings stay unchanged. Frappe validates header
    arithmetic before universal coercion; a string "0" is truthy there. Format
    Decimal tokens directly instead of converting money through binary floats.
    """
    numeric = {"qty", "rate", "price_list_rate", "discount_amount",
               "discount_percentage", "additional_discount_percentage", "conversion_rate", "tax_amount"}

    def encode(value, key=None):
        if key in numeric:
            if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
                raise ERPRejected("ERP_NUMERIC_WIRE_INVALID")
            try:
                number = Decimal(value)
                if not number.is_finite():
                    raise ValueError("nonfinite numeric field")
                return format(number, "f")
            except (ValueError, ArithmeticError) as error:
                raise ERPRejected("ERP_NUMERIC_WIRE_INVALID") from error
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(k) + ":" + encode(v, k) for k, v in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ",".join(encode(v) for v in value) + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    return encode(body).encode("utf-8")


class ERPNextClient:
    """REST adapter; live deployment must be separately tested.

    No submit/delete/payment tool exists. Bounded CNY/EA cost mappings require
    explicit rates/accounts and independent readback. Multi-item zero-tax terms
    are an offline adapter contract, not evidence of a live ERPNext round trip.
    """
    mode = "erpnext"
    key_field = "custom_procureflow_operation_key"
    hash_field = "custom_procureflow_snapshot_hash"

    def __init__(self, base_url: str, api_key: str, api_secret: str, company: str,
                 allow_writes=False, transport: httpx.BaseTransport | None = None, *,
                 tax_account="", freight_account=""):
        endpoint = validate_endpoint(base_url)
        if not api_key or not api_secret or not company:
            raise ValueError("ERP API credentials and company are required")
        self.company, self.allow_writes = company, allow_writes
        self.cost_accounts = {"tax": tax_account, "freight": freight_account}
        self.client = httpx.Client(base_url=endpoint, timeout=10, trust_env=False,
            follow_redirects=False, transport=transport,
            headers={"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"})

    def _call(self, method, path, *, envelope="data", **kwargs):
        # The read-only preflight cannot be converted to a write via another method.
        if method != "GET" and not self.allow_writes:
            raise ERPRejected("ERP_DRAFT_WRITES_DISABLED")
        if method not in {"GET", "POST"} or (method == "POST" and path != "api/resource/Supplier Quotation"):
            raise ERPRejected("ERP_METHOD_NOT_ALLOWED")
        try:
            import time
            status, decoded = request_json(self.client, method, path, limit=2 * 1024 * 1024,
                deadline=time.monotonic() + 20, parse_float=Decimal, **kwargs)
            if status >= 500 or status in {408, 429}:
                raise ERPUnknown("ERP_RESPONSE_UNCERTAIN")
            if status >= 300:
                raise ERPRejected(f"ERP_HTTP_{status}")
            if not isinstance(decoded, dict) or envelope not in decoded:
                raise ERPUnknown("ERP_MALFORMED_RESPONSE")
            return decoded[envelope]
        except ResponseLimitError as error:
            raise ERPUnknown("ERP_RESPONSE_LIMIT") from error
        except (httpx.HTTPError, ResponseDeadlineError) as error:
            raise ERPUnknown("ERP_TRANSPORT_UNCERTAIN") from error
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise ERPUnknown("ERP_MALFORMED_RESPONSE") from error

    def suppliers(self):
        # Bounded first page; not advertised as a complete supplier directory.
        return self._call("GET", "api/resource/Supplier", params={
            "fields": json.dumps(["name", "supplier_name"]), "limit_page_length": 100})

    def _normalize(self, record, expected_key=None):
        if not isinstance(record, dict) or not isinstance(record.get("items"), list):
            raise ERPUnknown("ERP_MALFORMED_DOCUMENT")
        items = record["items"]
        if not 1 <= len(items) <= MAX_ERP_LINES:
            raise ERPRejected("ERP_ITEM_COUNT_MISMATCH")
        if (any(not isinstance(item, dict) for item in items)
                or not isinstance(record.get("name"), str) or not record["name"]):
            raise ERPUnknown("ERP_MALFORMED_DOCUMENT")
        if expected_key is not None and record.get(self.key_field) != expected_key:
            raise ERPRejected("ERP_REMOTE_OPERATION_KEY_MISMATCH")
        skus = [item.get("item_code") for item in items]
        if (any(not isinstance(sku, str) or not sku or len(sku) > 80 for sku in skus)
                or len(set(skus)) != len(skus)):
            raise ERPRejected("ERP_ITEM_SET_INVALID")
        if type(record.get("docstatus")) is not int:
            raise ERPUnknown("ERP_MALFORMED_DOCSTATUS")
        try:
            total = Decimal(str(record.get("grand_total")))
            if not total.is_finite() or total < 0:
                raise ValueError("invalid total")
            for item in items:
                quantity, price = (Decimal(str(item.get(key))) for key in ("qty", "rate"))
                if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price < 0:
                    raise ValueError("invalid item amounts")
        except (ArithmeticError, ValueError, TypeError) as error:
            raise ERPUnknown("ERP_MALFORMED_AMOUNTS") from error
        multi_proof = self._multi_cost_proof(record)
        result = {"name": record["name"], "docstatus": record.get("docstatus"),
                "snapshot_hash": record.get(self.hash_field), "operation_key": record.get(self.key_field),
                "transaction_date": record.get("transaction_date"), "supplier_id": record.get("supplier"),
                "total": str(record.get("grand_total")), "currency": record.get("currency"),
                "company": record.get("company"), "simulated": False,
                "lines": [{"sku": item["item_code"], "quantity": str(item.get("qty")),
                           "uom": item.get("uom"), "unit_price": str(item.get("rate"))}
                          for item in sorted(items, key=lambda item: item["item_code"])],
                # A one-line multi contract is distinct from a legacy scalar
                # contract. find() cannot infer that intent from the row count.
                "multi_cost_proof": multi_proof,
                "cost_proof": self._cost_proof(record) if len(items) == 1 else multi_proof}
        if len(items) == 1:
            result.update(sku=items[0].get("item_code"), quantity=str(items[0].get("qty")),
                          uom=items[0].get("uom"), unit_price=str(items[0].get("rate")))
        return result

    @staticmethod
    def _multi_cost_proof(record):
        # Reuse only the independently observed header/tax rows. The legacy
        # extraction's first item is removed and replaced with every SKU below.
        header = ERPNextClient._cost_proof(record)
        proof = {key: value for key, value in header.items()
                 if not key.startswith("item_") and key != "price_list_rate"}
        proof["mapping_version"] = MULTI_COST_MAPPING_VERSION
        proof["lines"] = [{"sku": item.get("item_code"), "quantity": str(item.get("qty")),
            "uom": item.get("uom"), "unit_price": str(item.get("rate")),
            "line_terms": item.get("description"),
            "item_tax_template": optional_text(item.get("item_tax_template")),
            "item_pricing_rules": optional_text(item.get("pricing_rules")),
            "item_discount_amount": item.get("discount_amount"),
            "item_discount_percentage": item.get("discount_percentage"),
            "price_list_rate": item.get("price_list_rate"), "item_net_rate": item.get("net_rate"),
            "item_goods_total": item.get("amount"), "item_net_total": item.get("net_amount")}
            for item in sorted(record["items"], key=lambda item: item["item_code"])]
        return proof

    @staticmethod
    def _cost_proof(record):
        rows = record.get("taxes")
        if not isinstance(rows, list) or len(rows) > 2 or any(not isinstance(r, dict) for r in rows):
            raise ERPRejected("ERP_COST_ROWS_MISMATCH")
        return {"mapping_version": COST_MAPPING_VERSION,
            "taxes": [{key: ("0" if key == "rate" and row.get("charge_type") == "Actual" and row.get(key) is None
                else optional_text(row.get(key)) if key == "row_id" else row.get(key)) for key in ("category", "add_deduct_tax", "charge_type",
                "account_head", "description", "rate", "tax_amount", "included_in_print_rate",
                "tax_amount_after_discount_amount", "total", "row_id", "dont_recompute_tax")} for row in rows],
            **{key: optional_text(record.get(key)) for key in ("taxes_and_charges", "shipping_rule", "tax_category")},
            "pricing_rules": "" if record.get("pricing_rules") in (None, [], "") else record.get("pricing_rules"),
            "item_pricing_rules": optional_text(record["items"][0].get("pricing_rules")),
            "item_tax_template": optional_text(record["items"][0].get("item_tax_template")),
            "item_discount_amount": record["items"][0].get("discount_amount"),
            "item_discount_percentage": record["items"][0].get("discount_percentage"),
            "price_list_rate": record["items"][0].get("price_list_rate"),
            "item_net_rate": record["items"][0].get("net_rate"),
            "goods_total": record.get("total"), "item_goods_total": record["items"][0].get("amount"),
            "item_net_total": record["items"][0].get("net_amount"),
            **{key: record.get(key) for key in ("apply_discount_on", "discount_amount",
                "additional_discount_percentage", "net_total", "total_taxes_and_charges", "conversion_rate",
                "disable_rounded_total")}}

    def find(self, operation_key):
        self._check_company_currency()
        rows = self._call("GET", "api/resource/Supplier Quotation", params={
            "filters": json.dumps([[self.key_field, "=", operation_key]]),
            "fields": json.dumps(["name"]), "limit_page_length": 2})
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
            raise ERPUnknown("ERP_MALFORMED_SEARCH")
        if len(rows) > 1:
            raise ERPRejected("ERP_REMOTE_KEY_NOT_UNIQUE")
        if not rows:
            return None
        record = self._call("GET", "api/resource/Supplier Quotation/" + quote(rows[0]["name"], safe=""))
        return self._normalize(record, expected_key=operation_key)

    def _check_unique_field(self):
        fields = self._call("GET", "api/resource/Custom Field", params={
            "filters": json.dumps([["dt", "=", "Supplier Quotation"], ["fieldname", "=", self.key_field]]),
            "fields": json.dumps(["fieldname", "unique"]), "limit_page_length": 2})
        if not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict) or fields[0].get("fieldname") != self.key_field or fields[0].get("unique") != 1:
            raise ERPRejected("ERP_UNIQUE_FIELD_NOT_VERIFIED")

    def _check_snapshot_field(self):
        fields = self._call("GET", "api/resource/Custom Field", params={
            "filters": json.dumps([["dt", "=", "Supplier Quotation"], ["fieldname", "=", self.hash_field]]),
            "fields": json.dumps(["fieldname", "fieldtype"]), "limit_page_length": 2})
        if (not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict)
                or fields[0].get("fieldname") != self.hash_field or fields[0].get("fieldtype") != "Data"):
            raise ERPRejected("ERP_SNAPSHOT_FIELD_NOT_VERIFIED")

    def _check_company_currency(self):
        company = self._call("GET", "api/resource/Company/" + quote(self.company, safe=""))
        if not isinstance(company, dict) or company.get("name") != self.company:
            raise ERPRejected("ERP_COMPANY_NOT_VERIFIED")
        if company.get("default_currency") != "CNY":
            raise ERPRejected("ERP_COMPANY_CURRENCY_NOT_SUPPORTED")

    def preflight(self, expected_user: str | None = None):
        """Read-only capability probe; metadata is NOT proof of a working remote DB unique index."""
        if expected_user is not None:
            if not expected_user.strip() or expected_user.casefold() in {"administrator", "guest"}:
                raise ERPRejected("ERP_DEDICATED_IDENTITY_REQUIRED")
            logged_user = self._call("GET", "api/method/frappe.auth.get_logged_user", envelope="message")
            if logged_user != expected_user:
                raise ERPRejected("ERP_INTEGRATION_IDENTITY_MISMATCH")
        self._check_unique_field()
        self._check_snapshot_field()
        self._check_company_currency()
        suppliers = self.suppliers()
        if not isinstance(suppliers, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in suppliers):
            raise ERPUnknown("ERP_MALFORMED_SUPPLIERS")
        return {"status": "read_only_checks_passed", "company_verified": True, "company_currency_verified": True,
                "integration_identity_verified": expected_user is not None,
                "least_privilege_verified": False,
                "unique_field_metadata_verified": True, "snapshot_field_verified": True,
                "supplier_count_first_page": len(suppliers), "supplier_directory_complete": False, "returned_page_may_be_truncated": len(suppliers) >= 100,
                "draft_writes_enabled": self.allow_writes, "write_probe_performed": False,
                "remote_database_uniqueness_tested": False, "live_draft_roundtrip_verified": False}

    def _checked_result(self, remote, operation_key, payload):
        if not remote_matches(remote, payload, operation_key):
            raise ERPRejected("ERP_READBACK_PAYLOAD_MISMATCH")
        return remote

    def create_draft_guarded(self, operation_key, payload, before_write):
        return self.create_draft(operation_key, payload, before_write=before_write)

    def create_draft(self, operation_key, payload, *, before_write=None):
        if not self.allow_writes:
            raise ERPRejected("ERP_DRAFT_WRITES_DISABLED")
        if payload.get("erp_company") != self.company:
            raise ERPRejected("ERP_COMPANY_SNAPSHOT_MISMATCH")
        values = payload["quote_values"]
        accounts = payload.get("erp_cost_accounts", {"tax": "", "freight": ""})
        if accounts != self.cost_accounts:
            raise ERPRejected("ERP_COST_ACCOUNT_SNAPSHOT_MISMATCH")
        costs, _ = cost_mapping(payload)
        self._check_company_currency()
        self._check_unique_field()
        self._check_snapshot_field()
        if values.get("lines") is not None:
            checked_values = QuoteValues.model_validate(values)
            item_values = [{"item_code": line.sku, "qty": str(line.quantity), "uom": line.uom,
                           "rate": str(line.unit_price), "price_list_rate": str(line.unit_price),
                           "description": multi_line_description(line),
                           "discount_amount": "0", "discount_percentage": "0", "item_tax_template": ""}
                          for line in sorted(checked_values.lines, key=lambda line: line.sku)]
        else:
            item_values = [{"item_code": values["sku"], "qty": values["quantity"], "uom": values["uom"],
                "rate": values["unit_price"], "price_list_rate": values["unit_price"],
                "discount_amount": "0", "discount_percentage": "0", "item_tax_template": ""}]
        body = {"doctype": "Supplier Quotation", "docstatus": 0, "company": self.company,
                "supplier": values["supplier_id"], "transaction_date": payload["transaction_date"],
                "currency": values["currency"], self.key_field: operation_key,
                self.hash_field: payload["snapshot_hash"], "items": item_values, **costs}
        content = erp_numeric_json(body)
        # Company/field metadata checks above perform network I/O. Authority may
        # expire during them, so the caller's final gate belongs here, directly
        # before the first POST, while its business/identity locks remain held.
        if before_write is not None:
            before_write()
        try:
            record = self._call("POST", "api/resource/Supplier Quotation", content=content,
                headers={"Content-Type": "application/json"})
        except ERPRejected:
            # A conflicting unique insert can be somebody else's identical delivery.
            found = self.find(operation_key)
            if found:
                return self._checked_result(found, operation_key, payload)
            raise
        # Verify the persisted document with a separate GET, not merely the POST echo.
        if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"]:
            raise ERPUnknown("ERP_MALFORMED_CREATE_RECEIPT")
        persisted = self._call("GET", "api/resource/Supplier Quotation/" + quote(record["name"], safe=""))
        return self._checked_result(self._normalize(persisted, expected_key=operation_key), operation_key, payload)


def remote_matches(remote: dict, payload: dict, operation_key: str | None = None) -> bool:
    """Never treat an arbitrary object returned by a search as successful execution."""
    if not isinstance(remote, dict) or type(remote.get("simulated")) is not bool:
        return False
    try:
        values = payload["quote_values"]
        multi = values.get("lines") is not None
        if remote["simulated"] is False:
            _, expected_costs = cost_mapping(payload)
            if not cost_proof_matches(remote.get("multi_cost_proof" if multi else "cost_proof"), expected_costs):
                return False
        elif multi:
            if not cost_proof_matches(remote.get("cost_values"), expected_mock_cost_values(payload)):
                return False
        elif remote.get("cost_values") != expected_mock_cost_values(payload):
            return False
        common = ((operation_key is None or remote.get("operation_key") == operation_key)
                and isinstance(remote.get("name"), str) and bool(remote.get("name"))
                and type(remote.get("docstatus")) is int and remote.get("docstatus") == 0
                and remote.get("snapshot_hash") == payload["snapshot_hash"]
                and remote.get("supplier_id") == values["supplier_id"] and remote.get("currency") == values["currency"]
                and remote.get("company") == payload["erp_company"]
                and not isinstance(remote.get("total"), bool)
                and Decimal(remote["total"]).is_finite() and Decimal(remote["total"]) == Decimal(payload["total"])
                and remote.get("transaction_date") == payload["transaction_date"])
        if not common:
            return False
        if multi:
            rows = remote.get("lines")
            if (not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ERP_LINES
                    or any(not isinstance(row, dict) or not isinstance(row.get("sku"), str) for row in rows)
                    or len({row["sku"] for row in rows}) != len(rows)):
                return False
            return cost_proof_matches({"lines": sorted(rows, key=lambda row: row["sku"])},
                                      {"lines": quote_item_values(payload)})
        # Retain the exact legacy one-line match, and never ignore extra rows.
        if "lines" in remote and (not isinstance(remote["lines"], list) or len(remote["lines"]) != 1):
            return False
        return (remote.get("sku") == values["sku"] and remote.get("uom") == values["uom"]
                and not isinstance(remote.get("quantity"), bool) and not isinstance(remote.get("unit_price"), bool)
                and Decimal(remote["quantity"]).is_finite() and Decimal(remote["unit_price"]).is_finite()
                and Decimal(remote["quantity"]) == Decimal(values["quantity"])
                and Decimal(remote["unit_price"]) == Decimal(values["unit_price"]))
    except (ValueError, TypeError, KeyError, ArithmeticError, ERPRejected):
        return False
