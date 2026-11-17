"""Reversible, offline retention for expired table previews. Never removes source bytes.

The tenant anchor is the same serialization boundary used by uploads, mappings and
confirmation. Plans are bounded, content-addressed snapshots, not authorization
credentials; filesystem/database access remains an operator trust boundary.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from .db import DocumentRow, EventRow, RequestRow, TableImportRow, TenantPolicyRow, audit
from .errors import DomainError
from .policies import lock_tenant

DEFAULT_RETENTION_HOURS = 24
MAX_RETENTION_HOURS = 24 * 365
MAX_PLAN_ENTRIES = 1000
PLAN_TTL_MINUTES = 60


def timestamp(value):
    """Invalid/naive timestamps are never permission to expire or archive data."""
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _fingerprint(row):
    # All persisted fields, including mapping/evidence, participate in stale-plan checks.
    return _hash({column.name: getattr(row, column.name) for column in TableImportRow.__table__.columns})


def protected_reference_clause():
    """Keep linked evidence out of retention, including defensive shared-key checks.

    Document storage keys are global, even though operations are tenant scoped. A
    digest alone is only a reference within the same tenant/request. Another imported
    preview can also own a shared key. These checks never return other tenant data.
    """
    other = aliased(TableImportRow)
    return or_(TableImportRow.quote_id.is_not(None),
        exists(select(DocumentRow.id).where(or_(
            DocumentRow.storage_key == TableImportRow.storage_key,
            DocumentRow.id == "doc_" + func.substr(TableImportRow.id, 5),
            and_(DocumentRow.tenant_id == TableImportRow.tenant_id,
                 DocumentRow.request_id == TableImportRow.request_id,
                 DocumentRow.sha256 == TableImportRow.sha256)))),
        exists(select(other.id).where(other.id != TableImportRow.id,
            other.storage_key == TableImportRow.storage_key,
            or_(other.status == "IMPORTED", other.quote_id.is_not(None)))))


def pending_import_count(session, tenant_id, at=None):
    """Count usable previews, conservatively retaining corrupt/referenced OPEN rows.

    Caller must hold the tenant lock through the eventual upload or reopen commit.
    Python timestamp comparison accepts timezone offsets without unsafe lexical SQL.
    """
    at = at or _now()
    rows = session.execute(select(TableImportRow.expires_at, protected_reference_clause()).where(
        TableImportRow.tenant_id == tenant_id, TableImportRow.status == "OPEN"))
    return sum(1 for expiry, protected in rows
               if protected or (timestamp(expiry) is None or timestamp(expiry) > at))


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}", value):
        raise DomainError("RETENTION_IDENTIFIER_INVALID", "A bounded tenant/operator identifier is required", 422)
    return value


def _bounded(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise DomainError("RETENTION_PLAN_INVALID", "Retention settings or plan size are outside supported bounds", 422)
    return value


def _fail_stale():
    raise DomainError("RETENTION_PLAN_STALE", "The preview or its references changed; create and review a new plan")


class RetentionService:
    """Operator-only service; deliberately not exposed by the business HTTP API."""

    def __init__(self, db):
        self.db = db

    def plan(self, tenant_id, *, operation="archive", retention_hours=DEFAULT_RETENTION_HOURS,
             limit=100, import_ids=None):
        _identifier(tenant_id)
        _bounded(retention_hours, 1, MAX_RETENTION_HOURS)
        _bounded(limit, 1, MAX_PLAN_ENTRIES)
        if operation not in {"archive", "unarchive"}:
            raise DomainError("RETENTION_PLAN_INVALID", "Unsupported retention operation", 422)
        if operation == "unarchive" and not import_ids:
            raise DomainError("RETENTION_PLAN_INVALID", "Unarchive requires explicit preview IDs", 422)
        if import_ids is not None:
            if not isinstance(import_ids, list) or not 1 <= len(import_ids) <= limit:
                raise DomainError("RETENTION_PLAN_INVALID", "Preview IDs exceed the bounded plan size", 422)
            if any(not isinstance(value, str) or not re.fullmatch(r"tim_[a-f0-9]{32}", value) for value in import_ids):
                raise DomainError("RETENTION_PLAN_INVALID", "Invalid preview identifier", 422)
            if len(import_ids) != len(set(import_ids)):
                raise DomainError("RETENTION_PLAN_INVALID", "Repeated preview identifier", 422)
        generated = _now()
        cutoff = generated - timedelta(hours=retention_hours) if operation == "archive" else generated
        source_status = "OPEN" if operation == "archive" else "ARCHIVED"
        entries = []
        with self.db.transaction() as session:
            if session.get(TenantPolicyRow, tenant_id) is None:
                raise DomainError("NOT_FOUND", "Workspace not found", 404)
            query = select(TableImportRow).join(RequestRow, and_(RequestRow.id == TableImportRow.request_id,
                RequestRow.tenant_id == TableImportRow.tenant_id)).where(
                TableImportRow.tenant_id == tenant_id, TableImportRow.status == source_status,
                ~protected_reference_clause()).order_by(TableImportRow.id)
            if import_ids is not None:
                query = query.where(TableImportRow.id.in_(import_ids))
            for row in session.scalars(query).yield_per(100):
                expires = timestamp(row.expires_at)
                if expires is not None and expires <= cutoff:
                    entries.append({"import_id": row.id, "request_id": row.request_id,
                        "revision": row.revision, "expires_at": row.expires_at, "fingerprint": _fingerprint(row)})
                    if len(entries) == limit:
                        break
            if import_ids is not None and set(import_ids) != {entry["import_id"] for entry in entries}:
                raise DomainError("RETENTION_NOT_ELIGIBLE", "A selected preview is unavailable or ineligible in this workspace")
        plan = {"format_version": 1, "operation": operation, "tenant_id": tenant_id,
                "generated_at": generated.isoformat(),
                "valid_until": (generated + timedelta(minutes=PLAN_TTL_MINUTES)).isoformat(),
                "retention_hours": retention_hours, "limit": limit, "entries": entries}
        return {**plan, "plan_id": _hash(plan)}

    def _validate(self, plan, tenant_id):
        if not isinstance(plan, dict) or set(plan) != {"format_version", "operation", "tenant_id", "generated_at",
                "valid_until", "retention_hours", "limit", "entries", "plan_id"}:
            raise DomainError("RETENTION_PLAN_INVALID", "Unsupported retention plan", 422)
        _identifier(tenant_id)
        if plan["tenant_id"] != tenant_id:
            raise DomainError("RETENTION_TENANT_MISMATCH", "Plan does not match the explicitly selected workspace", 403)
        if plan["format_version"] != 1 or plan["operation"] not in {"archive", "unarchive"}:
            raise DomainError("RETENTION_PLAN_INVALID", "Unsupported retention plan", 422)
        _bounded(plan["retention_hours"], 1, MAX_RETENTION_HOURS)
        _bounded(plan["limit"], 1, MAX_PLAN_ENTRIES)
        if not isinstance(plan["entries"], list) or len(plan["entries"]) > plan["limit"]:
            raise DomainError("RETENTION_PLAN_INVALID", "Invalid plan entries", 422)
        ids = set()
        for entry in plan["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"import_id", "request_id", "revision", "expires_at", "fingerprint"}:
                raise DomainError("RETENTION_PLAN_INVALID", "Invalid plan entry", 422)
            if (not isinstance(entry["import_id"], str) or not re.fullmatch(r"tim_[a-f0-9]{32}", entry["import_id"])
                    or not isinstance(entry["request_id"], str) or not re.fullmatch(r"req_[a-f0-9]{32}", entry["request_id"])
                    or type(entry["revision"]) is not int or entry["revision"] < 1
                    or not isinstance(entry["fingerprint"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["fingerprint"])
                    or timestamp(entry["expires_at"]) is None or entry["import_id"] in ids):
                raise DomainError("RETENTION_PLAN_INVALID", "Invalid or repeated plan entry", 422)
            ids.add(entry["import_id"])
        if plan["plan_id"] != _hash({key: value for key, value in plan.items() if key != "plan_id"}):
            raise DomainError("RETENTION_PLAN_INVALID", "Plan fingerprint does not match its contents", 422)
        generated, until, current = timestamp(plan["generated_at"]), timestamp(plan["valid_until"]), _now()
        if (generated is None or until is None or generated > current or until <= current
                or until != generated + timedelta(minutes=PLAN_TTL_MINUTES)):
            raise DomainError("RETENTION_PLAN_EXPIRED", "Create and review a fresh retention plan")
        return generated - timedelta(hours=plan["retention_hours"]) if plan["operation"] == "archive" else generated

    def apply(self, plan, *, tenant_id, actor):
        cutoff = self._validate(plan, tenant_id)
        _identifier(actor)
        source, target = ("OPEN", "ARCHIVED") if plan["operation"] == "archive" else ("ARCHIVED", "OPEN")
        event_type = "TABLE_IMPORT_ARCHIVED" if target == "ARCHIVED" else "TABLE_IMPORT_UNARCHIVED"
        principal = SimpleNamespace(tenant_id=tenant_id, user_id=actor)
        changed, unchanged = [], []
        with self.db.transaction(write=True) as session:
            lock_tenant(session, tenant_id)
            # Lock order matches business writes: tenant -> request -> preview.
            for request_id in sorted({entry["request_id"] for entry in plan["entries"]}):
                if session.scalar(select(RequestRow.id).where(RequestRow.id == request_id,
                        RequestRow.tenant_id == tenant_id).with_for_update()) is None:
                    _fail_stale()
            for entry in plan["entries"]:
                pair = session.execute(select(TableImportRow, protected_reference_clause()).where(
                    TableImportRow.id == entry["import_id"], TableImportRow.tenant_id == tenant_id)
                    .with_for_update().execution_options(populate_existing=True)).first()
                if pair is None:
                    _fail_stale()
                row, protected = pair
                expires = timestamp(row.expires_at)
                if row.request_id != entry["request_id"] or protected or expires is None or expires > cutoff:
                    _fail_stale()
                fingerprint = _fingerprint(row)
                if row.status == target and row.revision == entry["revision"] + 1:
                    # Durable audit is the receipt after a lost response/restart. A
                    # later unarchive/reopen makes old plans stale rather than reapplying.
                    receipts = session.scalars(select(EventRow).where(EventRow.tenant_id == tenant_id,
                        EventRow.request_id == row.request_id, EventRow.type == event_type))
                    if any(event.payload.get("plan_id") == plan["plan_id"]
                           and event.payload.get("import_id") == row.id
                           and event.payload.get("before_fingerprint") == entry["fingerprint"]
                           and event.payload.get("after_fingerprint") == fingerprint for event in receipts):
                        unchanged.append(row.id)
                        continue
                if row.status != source or fingerprint != entry["fingerprint"]:
                    _fail_stale()
                row.status, row.revision = target, row.revision + 1
                audit(session, principal, row.request_id, event_type, {"import_id": row.id,
                    "plan_id": plan["plan_id"], "before_fingerprint": fingerprint,
                    "after_fingerprint": _fingerprint(row), "revision": row.revision,
                    "expires_at": row.expires_at, "source_bytes_preserved": True})
                changed.append(row.id)
            # Locks can wait. Do not commit a plan that expired during execution.
            self._validate(plan, tenant_id)
        return {"plan_id": plan["plan_id"], "operation": plan["operation"], "tenant_id": tenant_id,
                "changed": changed, "already_applied": unchanged, "files_deleted": 0}
