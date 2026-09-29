from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse
import httpx
from .domain import canonical, digest
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

    def create_draft(self, operation_key, payload):
        v = payload["quote_values"]
        if v["supplier_id"] not in {s["name"] for s in self.suppliers()}:
            raise ERPRejected("UNKNOWN_SUPPLIER")
        record = {"name": "MOCK-SQ-" + operation_key[:12].upper(), "docstatus": 0,
                  "snapshot_hash": payload["snapshot_hash"], "operation_key": operation_key, "supplier_id": v["supplier_id"],
                  "sku": v["sku"], "quantity": v["quantity"], "unit_price": v["unit_price"],
                  "transaction_date": payload["transaction_date"], "total": payload["total"],
                  "currency": v["currency"], "uom": v["uom"], "company": payload["erp_company"], "simulated": True}
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


class ERPNextClient:
    """REST adapter; live deployment must be separately tested.

    No submit/delete/payment tool exists. Alpha mapping accepts only explicit
    zero-tax, zero-freight, zero-discount single-line Supplier Quotations.
    Every other mapping fails closed rather than silently altering totals.
    """
    mode = "erpnext"
    key_field = "custom_procureflow_operation_key"
    hash_field = "custom_procureflow_snapshot_hash"

    def __init__(self, base_url: str, api_key: str, api_secret: str, company: str,
                 allow_writes=False, transport: httpx.BaseTransport | None = None):
        endpoint = validate_endpoint(base_url)
        if not api_key or not api_secret or not company:
            raise ValueError("ERP API credentials and company are required")
        self.company, self.allow_writes = company, allow_writes
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
        if len(items) != 1:
            raise ERPRejected("ERP_ITEM_COUNT_MISMATCH")
        if not isinstance(items[0], dict) or not isinstance(record.get("name"), str) or not record["name"]:
            raise ERPUnknown("ERP_MALFORMED_DOCUMENT")
        if type(record.get("docstatus")) is not int:
            raise ERPUnknown("ERP_MALFORMED_DOCSTATUS")
        if expected_key is not None and record.get(self.key_field) != expected_key:
            raise ERPRejected("ERP_REMOTE_OPERATION_KEY_MISMATCH")
        try:
            numbers = [Decimal(str(value)) for value in (items[0].get("qty"), items[0].get("rate"), record.get("grand_total"))]
            if not all(number.is_finite() for number in numbers) or numbers[0] <= 0 or any(n < 0 for n in numbers[1:]):
                raise ValueError("invalid amounts")
        except (ArithmeticError, ValueError, TypeError) as error:
            raise ERPUnknown("ERP_MALFORMED_AMOUNTS") from error
        return {"name": record["name"], "docstatus": record.get("docstatus"),
                "snapshot_hash": record.get(self.hash_field), "operation_key": record.get(self.key_field),
                "unit_price": str(items[0].get("rate")), "transaction_date": record.get("transaction_date"),
                "supplier_id": record.get("supplier"),
                "sku": items[0].get("item_code"), "quantity": str(items[0].get("qty")),
                "total": str(record.get("grand_total")), "currency": record.get("currency"),
                "company": record.get("company"), "uom": items[0].get("uom"), "simulated": False}

    def find(self, operation_key):
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
        company = self._call("GET", "api/resource/Company/" + quote(self.company, safe=""))
        if not isinstance(company, dict) or company.get("name") != self.company:
            raise ERPRejected("ERP_COMPANY_NOT_VERIFIED")
        suppliers = self.suppliers()
        if not isinstance(suppliers, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in suppliers):
            raise ERPUnknown("ERP_MALFORMED_SUPPLIERS")
        return {"status": "read_only_checks_passed", "company_verified": True,
                "integration_identity_verified": expected_user is not None,
                "least_privilege_verified": False,
                "unique_field_metadata_verified": True, "snapshot_field_verified": True,
                "supplier_count_first_page": len(suppliers), "supplier_directory_complete": False, "returned_page_may_be_truncated": len(suppliers) >= 100,
                "draft_writes_enabled": self.allow_writes, "write_probe_performed": False,
                "remote_database_uniqueness_tested": False, "live_draft_roundtrip_verified": False}

    def _checked_result(self, remote, operation_key, payload):
        if (remote.get("operation_key") != operation_key or not remote_matches(remote, payload, operation_key)
                or Decimal(remote["unit_price"]) != Decimal(payload["quote_values"]["unit_price"])
                or remote.get("transaction_date") != payload["transaction_date"]):
            raise ERPRejected("ERP_READBACK_PAYLOAD_MISMATCH")
        return remote

    def create_draft(self, operation_key, payload):
        if not self.allow_writes:
            raise ERPRejected("ERP_DRAFT_WRITES_DISABLED")
        if payload.get("erp_company") != self.company:
            raise ERPRejected("ERP_COMPANY_SNAPSHOT_MISMATCH")
        values = payload["quote_values"]
        try:
            if (values.get("tax_rate") is None or values.get("tax_mode") not in {"included", "excluded"}
                    or any(Decimal(str(values[key])) != 0 for key in ("tax_rate", "shipping_cost", "discount"))):
                raise ERPRejected("ERP_COMPLEX_TAX_FREIGHT_MAPPING_NOT_IMPLEMENTED")
            rate, quantity, total = (Decimal(str(value)) for value in (values["unit_price"], values["quantity"], payload["total"]))
            if not all(v.is_finite() for v in (rate, quantity, total)) or rate < 0 or quantity <= 0 or total < 0:
                raise ERPRejected("ERP_PAYLOAD_INVALID")
            if (rate * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != total:
                raise ERPRejected("ERP_PAYLOAD_TOTAL_MISMATCH")
        except (ValueError, TypeError, ArithmeticError, KeyError) as error:
            raise ERPRejected("ERP_PAYLOAD_INVALID") from error
        self._check_unique_field()
        self._check_snapshot_field()
        body = {"doctype": "Supplier Quotation", "docstatus": 0, "company": self.company,
                "supplier": values["supplier_id"], "transaction_date": payload["transaction_date"],
                "currency": values["currency"], self.key_field: operation_key,
                self.hash_field: payload["snapshot_hash"], "items": [{"item_code": values["sku"],
                "qty": values["quantity"], "uom": values["uom"], "rate": values["unit_price"]}]}
        try:
            record = self._call("POST", "api/resource/Supplier Quotation", json=body)
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
    values = payload["quote_values"]
    try:
        return ((operation_key is None or remote.get("operation_key") == operation_key)
                and isinstance(remote.get("name"), str) and bool(remote.get("name")) and type(remote.get("docstatus")) is int and remote.get("docstatus") == 0
                and remote.get("snapshot_hash") == payload["snapshot_hash"]
                and remote.get("supplier_id") == values["supplier_id"]
                and remote.get("sku") == values["sku"] and remote.get("currency") == values["currency"]
                and remote.get("uom") == values["uom"] and remote.get("company") == payload["erp_company"]
                and Decimal(remote["quantity"]) == Decimal(values["quantity"])
                and Decimal(remote["total"]) == Decimal(payload["total"])
                and Decimal(remote["unit_price"]) == Decimal(values["unit_price"])
                and remote.get("transaction_date") == payload["transaction_date"])
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return False
