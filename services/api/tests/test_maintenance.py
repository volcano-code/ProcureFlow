"""Synthetic-only maintenance fencing and restored no-replay gates."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.contracts import Principal
from procureflow.db import Database, EventRow, OperationRow, RecoveryHoldRow, SystemStateRow, now
from procureflow.errors import DomainError
from procureflow.maintenance import activity_lock, pause_writes, recovery_report, resume_writes
from procureflow.worker import drain_once, pending_work_query
from conftest import approved, enqueue, BUYER, OTHER


def resume(db, **extra):
    report = recovery_report(db)
    return resume_writes(db, generation=report['generation'], ledger_sha256=report['ledger_sha256'], **extra)


@pytest.mark.parametrize('exclusive', [False, True])
def test_postgres_activity_lock_namespace_is_bound_without_live_database(exclusive):
    statements = []
    class Connection:
        def execute(self, statement, parameters=None):
            compiled = statement.compile(dialect=postgresql.dialect())
            # Exercise SQLAlchemy's required-bind validation, as execute() does.
            statements.append((str(compiled), compiled.construct_params(parameters)))
    @contextmanager
    def transaction():
        yield Connection()
    db = SimpleNamespace(sqlite=False, fence_engine=SimpleNamespace(begin=transaction))
    with activity_lock(db, exclusive=exclusive):
        assert len(statements) == 2
    name = 'pg_advisory_xact_lock' if exclusive else 'pg_advisory_xact_lock_shared'
    assert statements == [
        ("SET LOCAL lock_timeout = '10s'", {}),
        (f"SELECT {name}(hashtextextended(current_database() || ':' || "
         "('system_state'::regclass)::oid::text || %(lock_namespace)s, 0))",
         {'lock_namespace': ':procureflow-maintenance-v1'}),
    ]


def test_pause_blocks_api_worker_and_direct_transactions_without_erp(system):
    client, service, erp = system
    request, _, proposal = approved(client)
    operation = enqueue(client, request, proposal)
    before = recovery_report(service.db)['operations']
    report = pause_writes(service.db)
    assert report['state'] == 'PAUSED'
    assert pause_writes(service.db)['generation'] == report['generation']
    assert drain_once(service) == 0
    assert erp.count() == 0
    assert recovery_report(service.db)['operations'] == before
    assert client.get(f"/api/v1/operations/{operation['id']}", headers=BUYER).status_code == 200
    response = client.post(f"/api/v1/requests/{request['id']}/execute", headers=BUYER,
                          json={'snapshot_hash': proposal['snapshot_hash']})
    assert response.status_code == 503 and response.json()['error']['code'] == 'WRITES_PAUSED'
    with pytest.raises(DomainError, match='paused'):
        service.process_pending_operation('demo', operation['id'])
    with pytest.raises(DomainError, match='paused'):
        with service.db.transaction(write=True):
            pytest.fail('write gate must fail before transaction body')
    assert client.get('/api/v1/capabilities', headers=BUYER).json()['erp_draft_writes_enabled'] is False
    assert resume(service.db)['state'] == 'ACTIVE'
    assert drain_once(service) == 1
    assert erp.count() == 1


def test_pause_survives_new_process_and_backup_requires_pause(system):
    _, service, _ = system
    with pytest.raises(DomainError) as error:
        with service.db.maintenance():
            pass
    assert error.value.code == 'MAINTENANCE_PAUSE_REQUIRED'
    pause_writes(service.db)
    db = Database(service.settings.database_url)
    try:
        assert db.state()['state'] == 'PAUSED'
        with pytest.raises(DomainError):
            db.assert_writable()
        with db.maintenance() as session:
            assert session.get(SystemStateRow, 1).state == 'PAUSED'
    finally:
        db.engine.dispose()


def test_paused_restart_no_bootstrap_mutations(system):
    _, service, erp = system
    pause_writes(service.db)
    before = recovery_report(service.db)
    with TestClient(create_app(service.settings, service.db, erp)) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/api/v1/auth/login', json={'credential': 'fake'}).status_code == 503
    assert recovery_report(service.db) == before


def test_resume_stale_review_and_recovery_acknowledgments_fail_closed(system):
    client, service, _ = system
    request, _, proposal = approved(client)
    operation = enqueue(client, request, proposal)
    pause_writes(service.db)
    with pytest.raises(DomainError) as error:
        resume_writes(service.db, generation=1, ledger_sha256='0' * 64)
    assert error.value.code == 'MAINTENANCE_REVIEW_STALE'
    with service.db.operator_transaction() as session:
        state = session.get(SystemStateRow, 1)
        state.state, state.restore_id, state.required_auth_mode = 'RECOVERY', 'restore-fixture', 'pilot'
        session.add(RecoveryHoldRow(operation_id=operation['id'], restore_id='restore-fixture',
            original_status='PENDING', created_at=now()))
    for extra in ({}, {'restore_id': 'wrong', 'acknowledge_reconciliation': True, 'acknowledge_credentials': True},
                  {'restore_id': 'restore-fixture', 'acknowledge_reconciliation': True}):
        with pytest.raises(DomainError) as error:
            resume(service.db, **extra)
        assert error.value.code == 'RECOVERY_REVIEW_REQUIRED'
    report = resume(service.db, restore_id='restore-fixture', acknowledge_reconciliation=True,
                    acknowledge_credentials=True)
    assert report['state'] == 'ACTIVE'
    assert report['required_auth_mode'] == 'pilot'
    assert report['operations'][0]['status'] == 'PENDING'  # evidence preserved exactly
    assert report['operations'][0]['outbox_status'] == 'PENDING'
    assert report['operations'][0]['hold_restore_id'] == 'restore-fixture'
    assert drain_once(service) == 0
    with pytest.raises(DomainError) as error:
        service.process_pending_operation('demo', operation['id'])
    assert error.value.code == 'RECOVERY_OPERATION_HELD'
    with pytest.raises(DomainError) as error:
        service.process_pending_operation('other', operation['id'])
    assert error.value.code == 'NOT_FOUND'
    with pytest.raises(DomainError) as error:
        create_app(service.settings, service.db, service.erp)
    assert error.value.code == 'RECOVERY_PILOT_REQUIRED'


def test_pause_waits_for_entire_external_call_and_receipt(system):
    client, service, erp = system
    request, _, proposal = approved(client)
    operation = enqueue(client, request, proposal)
    entered, release = threading.Event(), threading.Event()
    original = erp.find
    def delayed_find(key):
        entered.set()
        assert release.wait(5)
        return original(key)
    erp.find = delayed_find
    # Delay the read before first POST, after the IN_FLIGHT claim transaction.
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(service.process_pending_operation, 'demo', operation['id'])
        assert entered.wait(5)
        pausing = pool.submit(pause_writes, service.db)
        time.sleep(.1)
        assert not pausing.done()
        release.set()
        assert dispatch.result(timeout=5)['status'] == 'COMPLETED'
        report = pausing.result(timeout=5)
    assert report['operations'][0]['status'] == 'COMPLETED'
    assert report['state'] == 'PAUSED' and erp.count() == 1


def test_exclusive_snapshot_serializes_resume(system):
    _, service, _ = system
    pause_writes(service.db)
    entered, release = threading.Event(), threading.Event()
    def snapshot():
        with service.db.maintenance():
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(snapshot)
        assert entered.wait(5)
        resuming = pool.submit(resume, service.db)
        time.sleep(.1)
        assert not resuming.done()
        release.set()
        copying.result(timeout=5)
        assert resuming.result(timeout=5)['state'] == 'ACTIVE'


def test_missing_state_is_fail_closed(system):
    _, service, _ = system
    with service.db.operator_transaction() as session:
        session.delete(session.get(SystemStateRow, 1))
    with pytest.raises(DomainError) as error:
        service.db.assert_writable()
    assert error.value.code == 'MAINTENANCE_STATE_INVALID'


def test_activity_prevents_lock_upgrade_deadlock(system):
    _, service, _ = system
    with service.db.activity():
        with pytest.raises(RuntimeError, match='UPGRADE_FORBIDDEN'):
            pause_writes(service.db)


def test_snapshot_no_sqlite_symlink_lock(tmp_path):
    db = Database(f'sqlite:///{tmp_path}/db.sqlite3', create_schema=True)
    target = tmp_path / 'untouched'
    target.write_text('keep')
    (tmp_path / 'db.sqlite3.activity.lock').symlink_to(target)
    try:
        with pytest.raises(OSError):
            pause_writes(db)
        assert target.read_text() == 'keep'
    finally:
        db.engine.dispose()


def test_sqlite_alias_shares_fence_and_hardlinks_are_refused(tmp_path):
    original = tmp_path / 'real.sqlite3'
    alias = tmp_path / 'alias.sqlite3'
    real_db = Database(f'sqlite:///{original}', create_schema=True)
    alias.symlink_to(original)
    alias_db = Database(f'sqlite:///{alias}')
    entered, release = threading.Event(), threading.Event()
    def activity():
        with real_db.activity(write=True):
            entered.set()
            assert release.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            busy = pool.submit(activity)
            assert entered.wait(5)
            paused = pool.submit(pause_writes, alias_db)
            time.sleep(.1)
            assert not paused.done()
            release.set()
            busy.result(timeout=5)
            assert paused.result(timeout=5)['state'] == 'PAUSED'
        assert {p.name for p in tmp_path.glob('*.activity.lock')} == {'real.sqlite3.activity.lock'}
        os.link(original, tmp_path / 'hardlink.sqlite3')
        with pytest.raises(DomainError) as error:
            with real_db.activity():
                pass
        assert error.value.code == 'MAINTENANCE_DATABASE_ALIAS_DENIED'
    finally:
        real_db.engine.dispose()
        alias_db.engine.dispose()


def test_maintenance_evidence_reads_are_nonmutating(system):
    client, service, _ = system
    request, _, _ = approved(client)
    pause_writes(service.db)
    with service.db.transaction() as session:
        count = len(session.scalars(select(EventRow)).all())
    for suffix in ('approvals', 'evaluations', 'advice-runs'):
        response = client.get(f"/api/v1/requests/{request['id']}/{suffix}", headers=BUYER)
        assert response.status_code == 200, (suffix, response.text)
    with service.db.transaction() as session:
        assert len(session.scalars(select(EventRow)).all()) == count
    with pytest.raises(DomainError) as error:
        with service.db.transaction(consistent=True) as session:
            session.get(SystemStateRow, 1).state = 'ACTIVE'
    assert error.value.code == 'READ_ONLY_TRANSACTION'
    assert service.db.state()['state'] == 'PAUSED'


def test_incomplete_restore_startup_is_denied_even_if_state_active(system):
    _, service, erp = system
    (service.settings.data_dir / '.restore-incomplete').write_text('synthetic')
    with pytest.raises(DomainError) as error:
        create_app(service.settings, service.db, erp)
    assert error.value.code == 'RESTORE_INCOMPLETE'


@pytest.mark.postgres
def test_postgres_search_path_alias_shares_resolved_relation_fence(pg_database):
    from uuid import uuid4
    from sqlalchemy import text
    schema = 'pf_empty_' + uuid4().hex
    with pg_database.engine.begin() as connection:
        target = connection.execute(text('SELECT current_schema()')).scalar_one()
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    alias_url = pg_database.engine.url.update_query_dict({'options': f'-csearch_path={schema},{target}'})
    alias = Database(alias_url.render_as_string(hide_password=False))
    entered, release = threading.Event(), threading.Event()
    def activity():
        with pg_database.activity(write=True):
            entered.set()
            assert release.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            busy = pool.submit(activity)
            assert entered.wait(5)
            paused = pool.submit(pause_writes, alias)
            time.sleep(.15)
            assert not paused.done()
            release.set()
            busy.result(timeout=5)
            assert paused.result(timeout=5)['state'] == 'PAUSED'
    finally:
        alias.engine.dispose()
        with pg_database.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}"'))
