"""Offline maintenance coordination. No purge, automatic replay or remote writes."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import time

from sqlalchemy import select, text

from .db import EventRow, OperationRow, OutboxRow, RecoveryHoldRow, SystemStateRow, now
from .domain import digest
from .errors import DomainError

LOCK_TIMEOUT_SECONDS = 10


@contextmanager
def activity_lock(db, exclusive=False):
    if not db.sqlite:
        # One held DB connection covers gaps between business transactions and
        # the complete external call. A crash releases the transaction lock.
        with db.fence_engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            name = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
            # Resolve the actual singleton relation, not current_schema():
            # search_path may begin with an empty per-user namespace and fall
            # through to the same business tables used by another role.
            connection.execute(text(f"SELECT {name}(hashtextextended(current_database() || ':' || "
                "('system_state'::regclass)::oid::text || ':procureflow-maintenance-v1', 0))"))
            yield
        return
    import fcntl
    database = db.engine.url.database
    if not database or database == ":memory:":
        raise DomainError("MAINTENANCE_FILE_DATABASE_REQUIRED", "Use a file-backed SQLite database", 503)
    database_path = Path(database).resolve()
    if database_path.exists() and database_path.stat().st_nlink != 1:
        raise DomainError("MAINTENANCE_DATABASE_ALIAS_DENIED", "Hard-linked SQLite files are unsupported", 503)
    path = database_path.with_name(database_path.name + ".activity.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DomainError("MAINTENANCE_LOCK_INVALID", "Invalid maintenance lock file", 503)
        limit = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= limit:
                    raise DomainError("MAINTENANCE_BUSY", "Active work has not quiesced; retry maintenance later", 503)
                time.sleep(0.02)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def recovery_report(db):
    """Offline evidence inventory; deliberately no ERP adapter or credentials."""
    with db.transaction() as session:
        state = session.get(SystemStateRow, 1)
        if state is None:
            raise DomainError("MAINTENANCE_STATE_INVALID", "Missing maintenance state", 503)
        operations = [dict(row) for row in session.execute(select(
            OperationRow.id, OperationRow.tenant_id, OperationRow.request_id,
            OperationRow.status, OperationRow.snapshot_hash, OperationRow.attempts,
            OperationRow.remote_id, OperationRow.error, OperationRow.lease_until,
            OutboxRow.status.label("outbox_status"), RecoveryHoldRow.restore_id.label("hold_restore_id"))
            .outerjoin(OutboxRow, OutboxRow.operation_id == OperationRow.id)
            .outerjoin(RecoveryHoldRow, RecoveryHoldRow.operation_id == OperationRow.id)
            .order_by(OperationRow.id)).mappings()]
        return {"state": state.state, "generation": state.generation, "reason": state.reason,
            "restore_id": state.restore_id, "required_auth_mode": state.required_auth_mode,
            "operations": operations, "ledger_sha256": digest(operations),
            "automatic_replay_enabled": False if state.state != "ACTIVE" or any(
                row["hold_restore_id"] for row in operations) else None}


def pause_writes(db, reason="operator_backup"):
    with db.activity(exclusive=True):
        with db.operator_transaction() as session:
            state = session.get(SystemStateRow, 1)
            if state is None or state.state not in {"ACTIVE", "PAUSED", "RECOVERY"}:
                raise DomainError("MAINTENANCE_STATE_INVALID", "Missing or invalid maintenance state", 503)
            if state.state == "ACTIVE":
                state.state, state.reason, state.updated_at = "PAUSED", reason[:120], now()
                state.generation += 1
                session.add(EventRow(tenant_id="__operator__", actor_id="offline-operator", request_id=None,
                    type="MAINTENANCE_PAUSED", payload={"generation": state.generation}))
        return recovery_report(db)


def resume_writes(db, *, generation, ledger_sha256, restore_id=None,
                  acknowledge_reconciliation=False, acknowledge_credentials=False):
    """Explicit local review enables new work; restored-operation holds remain."""
    with db.activity(exclusive=True):
        report = recovery_report(db)
        if generation != report["generation"] or ledger_sha256 != report["ledger_sha256"]:
            raise DomainError("MAINTENANCE_REVIEW_STALE", "Re-read the ledger before resuming", 409)
        if report["state"] == "RECOVERY":
            if (not restore_id or restore_id != report["restore_id"] or
                    not acknowledge_reconciliation or not acknowledge_credentials):
                raise DomainError("RECOVERY_REVIEW_REQUIRED",
                    "Review ERP uncertainty and revoke old credentials before enabling new pilot work", 409)
        elif report["state"] != "PAUSED":
            raise DomainError("MAINTENANCE_NOT_PAUSED", "Writes are not paused", 409)
        with db.operator_transaction() as session:
            state = session.get(SystemStateRow, 1)
            previous = state.state
            state.state, state.reason, state.updated_at = "ACTIVE", "operator_reviewed", now()
            state.generation += 1
            session.add(EventRow(tenant_id="__operator__", actor_id="offline-operator", request_id=None,
                type="RECOVERY_REVIEWED" if previous == "RECOVERY" else "MAINTENANCE_RESUMED",
                payload={"restore_id": state.restore_id, "generation": state.generation,
                    "ledger_sha256": ledger_sha256, "restored_operations_remain_held": True,
                    "new_work_requires_pilot": state.required_auth_mode == "pilot"}))
        return recovery_report(db)
