"""Read-only recovery diagnostics. No release, replay, acknowledgement or ERP writes.

A verified ERP receipt is only an observation; permanent recovery holds and all
approval/identity guards remain authoritative. No arbitrary payload or upstream
exception text is returned to the browser.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import re
import stat

from sqlalchemy import func, select

from .db import (ApprovalRow, DocumentRow, OperationRow, OutboxRow, QuoteRow,
                 QuoteVersionRow, RecoveryHoldRow, RequestRow, SystemStateRow, now)
from .domain import digest
from .erp import ERPRejected, cost_mapping, remote_matches, expected_mock_cost_values
from .errors import DomainError

MAX_SOURCE_BYTES = 16 * 1024 * 1024  # Same upper bound as recovery bundles.
_DECIMALS = {"quantity", "unit_price", "total", "tax_rate", "shipping_cost", "discount",
    "rate", "tax_amount", "tax_amount_after_discount_amount", "discount_amount",
    "additional_discount_percentage", "net_total", "total_taxes_and_charges",
    "goods_total", "item_goods_total", "item_net_total", "conversion_rate", "goods", "added_tax", "shipping",
    "item_discount_amount", "item_discount_percentage", "price_list_rate", "item_net_rate"}


def _safe_scalar(value):
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str) and len(value) <= 200:
        return value
    return "[invalid or oversized value]"


def expected_record(payload, operation_id, remote_id=None):
    values = payload.get("quote_values")
    values = values if isinstance(values, dict) else {}
    result = {key: _safe_scalar(values.get(key)) for key in
              ("supplier_id", "sku", "quantity", "unit_price", "currency", "uom")}
    result.update(operation_key=operation_id, snapshot_hash=_safe_scalar(payload.get("snapshot_hash")),
        total=_safe_scalar(payload.get("total")), company=_safe_scalar(payload.get("erp_company")),
        transaction_date=_safe_scalar(payload.get("transaction_date")), docstatus=0,
        simulated=payload.get("erp_mode") == "mock")
    if values.get("lines") is not None:
        for key in ("sku", "quantity", "unit_price", "uom"):
            result.pop(key, None)
        result["lines"] = _safe_lines(values.get("lines"))
    if remote_id is not None:
        result["name"] = remote_id
    return result


def _safe_lines(lines):
    if not isinstance(lines, list) or not 1 <= len(lines) <= 20 or any(not isinstance(line, dict) for line in lines):
        return "[invalid line collection]"
    return sorted(({key: _safe_scalar(line.get(key)) for key in ("sku", "quantity", "unit_price", "uom")}
                   for line in lines), key=lambda line: str(line["sku"]))


def _source_integrity(directory: Path, key, expected_hash):
    # Never follow a database-supplied path or symlink outside the paired sources.
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", key):
        return "unavailable"
    try:
        with os.fdopen(os.open(directory / key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_SOURCE_BYTES:
                return "unavailable"
            content = stream.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(content) > MAX_SOURCE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            return "unavailable"
        return "verified" if hashlib.sha256(content).hexdigest() == expected_hash else "mismatch"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"


def _reference(value):
    return value if isinstance(value, str) and 0 < len(value) <= 100 else None


def _held(session, tenant_id, operation_id):
    row = session.execute(select(OperationRow, RecoveryHoldRow, RequestRow, OutboxRow)
        .join(RecoveryHoldRow, RecoveryHoldRow.operation_id == OperationRow.id)
        .join(RequestRow, RequestRow.id == OperationRow.request_id)
        .outerjoin(OutboxRow, OutboxRow.operation_id == OperationRow.id)
        .where(OperationRow.id == operation_id, OperationRow.tenant_id == tenant_id,
               RequestRow.tenant_id == tenant_id)).first()
    if row is None:
        raise DomainError("NOT_FOUND", "Held operation not found in this workspace", 404)
    return row


def operation_summary(row):
    operation, hold, request, outbox = row
    return {"id": operation.id, "request_id": operation.request_id,
        "request_title": request.data.get("title", ""), "status": operation.status,
        "attempts": operation.attempts, "remote_id": operation.remote_id, "error": operation.error,
        "snapshot_hash": operation.snapshot_hash, "created_at": operation.created_at,
        "outbox_status": outbox.status if outbox else None,
        "hold": {"restore_id": hold.restore_id, "original_status": hold.original_status, "created_at": hold.created_at},
        "replay_permitted": False}


def ledger_digest(row):
    operation, _, _, _ = row
    return digest({**operation_summary(row), "payload_sha256": digest(operation.payload),
        "approval_id": operation.approval_id, "initiator_id": operation.initiator_id,
        "initiator_auth_version": operation.initiator_auth_version, "lease_until": operation.lease_until})


def operation_detail(session, row, document_dir, authority_current=None):
    operation, _, _, _ = row
    payload = operation.payload if isinstance(operation.payload, dict) else {}
    diagnostics = ["RECOVERY_HOLD_PERMANENT", "OBSERVATION_NEVER_AUTHORIZES_REPLAY"]
    approval = session.scalar(select(ApprovalRow).where(ApprovalRow.id == operation.approval_id,
        ApprovalRow.tenant_id == operation.tenant_id, ApprovalRow.request_id == operation.request_id))
    approval_view = None
    if approval:
        try:
            stamp = datetime.fromisoformat(approval.expires_at)
            unexpired = stamp.tzinfo is not None and stamp > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            unexpired = False
        authority = bool(authority_current and authority_current(approval))
        approval_view = {"id": approval.id, "status": approval.status, "expires_at": approval.expires_at,
            "authority_current": authority, "snapshot_matches": approval.snapshot_hash == operation.snapshot_hash,
            "unexpired": unexpired, "authority_evaluated": authority_current is not None}
        if not authority:
            diagnostics.append("APPROVER_AUTHORITY_NOT_CURRENT" if authority_current else "APPROVER_AUTHORITY_NOT_EVALUATED_OFFLINE")
        if not unexpired:
            diagnostics.append("APPROVAL_EXPIRED")
        if approval.status != "APPROVED" or not approval_view["snapshot_matches"]:
            diagnostics.append("APPROVAL_NOT_CURRENT")
    else:
        diagnostics.append("APPROVAL_MISSING")
    if payload.get("snapshot_hash") != operation.snapshot_hash or payload.get("snapshot_hash") != digest(
            {key: value for key, value in payload.items() if key != "snapshot_hash"}):
        diagnostics.append("LOCAL_SNAPSHOT_INTEGRITY_FAILED")
    sources = []
    quotes = payload.get("quote_collection", [])
    if not isinstance(quotes, list) or not quotes or len(quotes) > 100:
        diagnostics.append("SOURCE_BINDING_INVALID")
        quotes = []
    for quote in quotes:
        if not isinstance(quote, dict):
            diagnostics.append("SOURCE_BINDING_INVALID")
            continue
        document = session.scalar(select(DocumentRow).where(DocumentRow.id == _reference(quote.get("document_id")),
            DocumentRow.tenant_id == operation.tenant_id, DocumentRow.request_id == operation.request_id))
        version = session.scalar(select(QuoteVersionRow).join(QuoteRow, QuoteRow.id == QuoteVersionRow.quote_id)
            .where(QuoteVersionRow.id == _reference(quote.get("version_id")), QuoteRow.tenant_id == operation.tenant_id,
                   QuoteRow.request_id == operation.request_id))
        bound = (document is not None and version is not None and version.quote_id == quote.get("id")
            and version.version == quote.get("version") and version.document_id == document.id
            and document.sha256 == quote.get("document_sha256")
            and version.values == quote.get("values") and version.evidence == quote.get("evidence"))
        integrity = _source_integrity(document_dir, document.storage_key, document.sha256) if bound else "unavailable"
        sources.append({"id": document.id if document else None,
            "filename": document.filename if document else "Unavailable source", "sha256": document.sha256 if document else None,
            "quote_id": _safe_scalar(quote.get("id")), "quote_version": _safe_scalar(quote.get("version")), "integrity": integrity})
        if integrity != "verified":
            diagnostics.append("SOURCE_" + integrity.upper())
    return {**operation_summary(row), "payload_sha256": digest(operation.payload), "ledger_sha256": ledger_digest(row),
        "expected": expected_record(payload, operation.id, operation.remote_id), "approval": approval_view,
        "sources": sources, "diagnostics": list(dict.fromkeys(diagnostics))}


def offline_recovery_detail(db, document_dir, tenant_id, operation_id):
    """Explicit local operator evidence read, usable while restored logins are revoked."""
    with db.activity():
        with db.transaction(consistent=True) as session:
            return operation_detail(session, _held(session, tenant_id, operation_id), Path(document_dir))


def _equal(field, actual, expected):
    if actual is None or expected is None:
        return actual is expected
    if type(actual) is bool or type(expected) in (bool, int):
        return type(actual) is type(expected) and actual == expected
    if field in _DECIMALS:
        try:
            left, right = Decimal(str(actual)), Decimal(str(expected))
            return left.is_finite() and right.is_finite() and left == right
        except (ValueError, ArithmeticError):
            return False
    return type(actual) is type(expected) and actual == expected


def comparison(remote, payload, operation_id, remote_id):
    """Bounded allowlisted differences, including cost components with equal totals."""
    expected = expected_record(payload, operation_id, remote_id)
    observed = {key: _safe_lines(remote.get(key)) if key == "lines" else _safe_scalar(remote.get(key)) for key in expected}
    if "name" not in observed:
        observed["name"] = _safe_scalar(remote.get("name"))
    differences = []
    def diff(field, wanted, actual, reason="VALUE_MISMATCH"):
        differences.append({"field": field, "expected": _safe_scalar(wanted),
                            "observed": _safe_scalar(actual), "reason": reason})
    for key, value in expected.items():
        if key != "lines" and not _equal(key, remote.get(key), value):
            diff(key, value, remote.get(key))
    if not isinstance(remote.get("name"), str) or not remote["name"] or len(remote["name"]) > 120:
        diff("name", "non-empty ERP document ID (at most 120 characters)", remote.get("name"), "INVALID_REMOTE_ID")
    # Cost proof keys come only from the trusted local mapping. Unknown upstream
    # fields are counted, never echoed as free-form paths or document prose.
    if payload["erp_mode"] == "mock":
        wanted_costs = expected_mock_cost_values(payload)
        cost_key = "cost_values"
    else:
        _, wanted_costs = cost_mapping(payload)
        cost_key = "multi_cost_proof" if payload["quote_values"].get("lines") is not None else "cost_proof"
    def costs(path, wanted, actual):
        if isinstance(wanted, dict):
            if not isinstance(actual, dict):
                diff(path, "expected cost object", None, "INVALID_COST_PROOF")
                return
            if set(actual) != set(wanted):
                diff(path + ".keys", len(wanted), len(actual), "COST_FIELDS_MISMATCH")
            for key, value in wanted.items():
                costs(path + "." + key, value, actual.get(key))
        elif isinstance(wanted, list):
            if not isinstance(actual, list) or len(actual) != len(wanted):
                diff(path + ".length", len(wanted), len(actual) if isinstance(actual, list) else None, "COST_ROWS_MISMATCH")
            for index, value in enumerate(wanted):
                costs(f"{path}[{index}]", value, actual[index] if isinstance(actual, list) and index < len(actual) else None)
        elif not _equal(path.rsplit(".", 1)[-1], actual, wanted):
            diff(path, wanted, actual)
    if "lines" in expected:
        costs("lines", expected["lines"], observed["lines"])
    costs(cost_key, wanted_costs, remote.get(cost_key))
    return expected, observed, differences


class RecoveryServiceMixin:
    def recovery_operations(self, principal, *, after=None, limit=50):
        if not 1 <= limit <= 100 or (after is not None and not re.fullmatch(r"[a-f0-9]{64}", after)):
            raise DomainError("INVALID_RECOVERY_PAGE", "Use a valid cursor and page size", 422)
        with self.transaction(principal, consistent=True) as session:
            query = select(OperationRow, RecoveryHoldRow, RequestRow, OutboxRow).join(
                RecoveryHoldRow, RecoveryHoldRow.operation_id == OperationRow.id).join(
                RequestRow, RequestRow.id == OperationRow.request_id).outerjoin(
                OutboxRow, OutboxRow.operation_id == OperationRow.id).where(
                OperationRow.tenant_id == principal.tenant_id, RequestRow.tenant_id == principal.tenant_id)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            if after:
                query = query.where(OperationRow.id > after)
            rows = session.execute(query.order_by(OperationRow.id).limit(limit + 1)).all()
            state = session.get(SystemStateRow, 1)
            return {"state": {key: getattr(state, key) for key in ("state", "generation", "required_auth_mode", "restore_id")},
                "items": [operation_summary(row) for row in rows[:limit]], "total": total,
                "next_after": rows[limit - 1][0].id if len(rows) > limit else None}

    def recovery_operation(self, principal, operation_id):
        with self.transaction(principal, consistent=True) as session:
            row = _held(session, principal.tenant_id, operation_id)
            return operation_detail(session, row, self.document_dir, lambda approval:
                self.identities.approver_valid(session, principal.tenant_id, approval.approver_id,
                    auth_version=approval.approver_auth_version))

    def reconcile_recovery_operation(self, principal, operation_id):
        # Unlike /verify this path never writes an audit receipt and is safe for
        # existing authenticated readers while ordinarily paused. It does not
        # provide a login bypass to restored users, nor a route to resume writes.
        with self.db.activity():
            with self.transaction(principal, consistent=True) as session:
                row = _held(session, principal.tenant_id, operation_id)
                payload, expected_id, ledger = deepcopy(row[0].payload), row[0].remote_id, ledger_digest(row)
                snapshot = row[0].snapshot_hash
            receipt = {"operation_id": operation_id, "ledger_sha256": ledger, "verified_at": now(),
                "status": "unavailable", "matches_snapshot": False, "draft_verified": False,
                "network_attempted": False, "external_write_attempted": False, "replay_permitted": False,
                "simulated": self.erp.mode == "mock", "remote_id": None,
                "expected": expected_record(payload if isinstance(payload, dict) else {}, operation_id, expected_id),
                "observed": None, "differences": []}
            try:
                target_matches = isinstance(payload, dict) and payload.get("snapshot_hash") == snapshot and self._execution_target_matches(payload)
            except (TypeError, ValueError, KeyError):
                target_matches = False
            if not target_matches:
                receipt.update(status="blocked", reason="EXECUTION_TARGET_OR_SNAPSHOT_CHANGED")
            else:
                try:
                    receipt["network_attempted"] = self.erp.mode != "mock"
                    remote = self.erp.find(operation_id)
                    if remote is None:
                        receipt.update(status="missing", reason="REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT")
                    elif not isinstance(remote, dict):
                        receipt.update(status="unavailable", reason="ERP_MALFORMED_RESPONSE")
                    else:
                        expected, observed, differences = comparison(remote, payload, operation_id, expected_id)
                        receipt.update(expected=expected, observed=observed, differences=differences)
                        if not differences and remote_matches(remote, payload, operation_id):
                            receipt.update(status="verified", matches_snapshot=True, draft_verified=True, remote_id=remote["name"])
                        else:
                            receipt.update(status="mismatch", reason="REMOTE_PAYLOAD_MISMATCH")
                            if not differences:
                                receipt["differences"] = [{"field": "cost_contract", "expected": "exact persisted cost contract",
                                    "observed": "invalid", "reason": "REMOTE_CONTRACT_MISMATCH"}]
                except ERPRejected:
                    receipt.update(status="mismatch", reason="ERP_DOCUMENT_REJECTED")
                except Exception:
                    receipt.update(status="unavailable", reason="ERP_VERIFICATION_UNAVAILABLE")
            # Revalidate after network I/O. Revoked/expired readers receive no
            # protected response; a concurrent ledger change is never certified.
            with self.transaction(principal, consistent=True) as session:
                current = _held(session, principal.tenant_id, operation_id)
                if ledger_digest(current) != ledger:
                    receipt.update(status="blocked", reason="LOCAL_LEDGER_CHANGED_DURING_READ",
                                   matches_snapshot=False, draft_verified=False)
            return receipt
