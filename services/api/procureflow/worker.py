"""Durable local outbox drain. No Redis/Celery claim is made by this alpha.

Run once: python -m procureflow.worker --once
Run continuously in your own terminal: python -m procureflow.worker
"""
from __future__ import annotations
import argparse
import time
from sqlalchemy import select
from .app import app
from .contracts import Principal
from .db import OperationRow, OutboxRow


def drain_once():
    service = app.state.service
    with service.db.transaction() as session:
        work = list(session.execute(select(OperationRow.id, OperationRow.tenant_id)
            .join(OutboxRow, OutboxRow.operation_id == OperationRow.id)
            .where(OutboxRow.status == "PENDING").limit(50)))
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
