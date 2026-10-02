"""Durable local outbox drain. No Redis/Celery claim is made by this alpha.

Run once: python -m procureflow.worker --once
Run continuously in your own terminal: python -m procureflow.worker
"""
from __future__ import annotations
import argparse
import time
from sqlalchemy import and_, or_, select
from .contracts import Principal
from .db import OperationRow, OutboxRow, now


def pending_work_query(limit=50):
    """Do not let 50 actively leased operations starve later ready work.

    This is selection, not a claim. process_operation claims under the existing
    request-row lock and persists the lease before network I/O.
    """
    return (select(OperationRow.id, OperationRow.tenant_id)
        .join(OutboxRow, OutboxRow.operation_id == OperationRow.id)
        .where(OutboxRow.status == "PENDING", or_(
            OperationRow.status.in_(["PENDING", "RECONCILING"]),
            and_(OperationRow.status == "IN_FLIGHT", or_(
                OperationRow.lease_until.is_(None), OperationRow.lease_until <= now()))))
        .order_by(OutboxRow.created_at, OutboxRow.id).limit(limit))


def drain_once(service=None):
    if service is None:
        from .app import app
        service = app.state.service
    with service.db.transaction() as session:
        work = list(session.execute(pending_work_query()))
    for operation_id, tenant_id in work:
        result = service.process_operation(Principal(user_id="outbox-worker", tenant_id=tenant_id, role="buyer"), operation_id)
        print(f"{operation_id[:12]} {result['status']}", flush=True)
    return len(work)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        drain_once()
        if args.once:
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
