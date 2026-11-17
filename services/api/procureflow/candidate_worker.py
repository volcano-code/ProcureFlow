"""Independent advisory queue worker; no ERP dispatch, no ambiguous-call retries.

Default disabled. Configure credentials only on this trusted process. API enqueue
needs PF_ALLOW_CANDIDATE_QUEUE=1 but never needs the provider secret.
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from sqlalchemy import and_, or_, select
from .candidate_agent import CandidateAgent
from .candidates import CandidateService
from .db import QuoteCandidateRunRow, now
from .errors import DomainError


def pending_query(limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('INVALID_CANDIDATE_BATCH_LIMIT')
    return (select(QuoteCandidateRunRow.tenant_id, QuoteCandidateRunRow.id)
        .where(or_(QuoteCandidateRunRow.status == 'QUEUED', and_(
            QuoteCandidateRunRow.status == 'RUNNING', or_(
                QuoteCandidateRunRow.lease_until.is_(None), QuoteCandidateRunRow.lease_until <= now()))))
        .order_by(QuoteCandidateRunRow.created_at, QuoteCandidateRunRow.id).limit(limit))


def configured():
    return (os.getenv('PF_ALLOW_CANDIDATE_MODEL') == '1'
            and bool(os.getenv('LLM_API_KEY')) and bool(os.getenv('LLM_MODEL')))


def drain_once(service: CandidateService, *, factory=None, limit=20):
    if factory is None:
        if not configured():
            raise DomainError('CANDIDATE_WORKER_DISABLED', 'Explicit provider opt-in and configuration required', 503)
        factory = CandidateAgent.from_env
    try:
        service.db.assert_writable()
    except DomainError as exc:
        if exc.code == 'WRITES_PAUSED':
            return []
        raise
    with service.db.transaction() as session:
        work = list(session.execute(pending_query(limit)))
    receipts = []
    for tenant_id, run_id in work:
        try:
            receipts.append(service.dispatch(tenant_id, run_id, factory))
        except DomainError as exc:
            if exc.code == 'WRITES_PAUSED':
                break
            # No exception text/source content/secrets in the worker's stdout.
            receipts.append({'id': run_id, 'status': 'DISPATCH_BLOCKED'})
    return receipts


def build_service():
    # Do NOT import app: constructing an ERP client is unnecessary for this role.
    from .config import Settings
    from .db import Database
    from .service import ProcurementService
    settings = Settings()
    db = Database(settings.database_url, create_schema=False)
    db.check_ready(require_migrations=True)
    class NoERP:
        def __getattr__(self, name):
            raise RuntimeError('CANDIDATE_WORKER_HAS_NO_ERP_PORT')
    return CandidateService(ProcurementService(db, settings, NoERP()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=3.0)
    args = parser.parse_args(argv)
    if not 0.2 <= args.poll_seconds <= 60:
        parser.error('--poll-seconds must be between 0.2 and 60')
    if not configured():
        print('CANDIDATE_WORKER_DISABLED', file=sys.stderr)
        return 2
    service = build_service()
    try:
        while True:
            for receipt in drain_once(service):
                print(receipt['id'][:12], receipt['status'], flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        service.db.engine.dispose()
        if service.db.fence_engine is not None:
            service.db.fence_engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
