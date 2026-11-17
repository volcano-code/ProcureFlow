"""Queue/identity regressions. Scripted candidates are NOT model quality evidence."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from pathlib import Path
import os
import subprocess
import sys
import pytest
from sqlalchemy import func, select
from conftest import BUYER, APPROVER, AUDITOR, OTHER, publish_policy
from test_quote_candidates import FakeCandidate, make, import_run, reserve, source, TEXT
from procureflow.candidates import CandidateService
from procureflow.candidate_agent import CandidateAgent
from procureflow.candidate_worker import drain_once, pending_query
from procureflow.db import QuoteCandidateRunRow, QuoteRow, OperationRow
from procureflow.errors import DomainError


@pytest.fixture
def queued(monkeypatch):
    monkeypatch.setenv('PF_ALLOW_CANDIDATE_QUEUE', '1')
    return FakeCandidate()


def enqueue(client, run, headers=BUYER, expected=202, **extra):
    response = client.post(f"/api/v1/candidate-runs/{run['id']}/enqueue", headers=headers,
        json={'expected_revision': run['revision'], 'acknowledge_external_model': True, **extra})
    assert response.status_code == expected, response.text
    return response.json()


def stored(service, run):
    with service.db.transaction() as session:
        row = session.get(QuoteCandidateRunRow, run['id'])
        return row.status, row.output, row.dispatch_authority


def test_enqueue_returns_without_constructing_model_then_independent_dispatch(system, queued, monkeypatch):
    client, p, _ = system
    _, _, run = make(system)
    monkeypatch.setattr(CandidateAgent, 'from_env', staticmethod(lambda: pytest.fail('API called model')))
    accepted = enqueue(client, run)
    assert accepted['status'] == 'QUEUED' and accepted['queued_at'] and queued.calls == 0
    assert accepted['dispatch_mode'] == 'independent_worker' and 'dispatch_authority' not in accepted
    result = drain_once(CandidateService(p), factory=lambda: queued)
    assert result == [{'id': run['id'], 'status': 'NEEDS_REVIEW'}]
    assert queued.calls == 1 and drain_once(CandidateService(p), factory=lambda: queued) == []
    done = client.get(f"/api/v1/candidate-runs/{run['id']}", headers=BUYER).json()
    q = import_run(client, done)
    assert q['confirmed_by'] is None
    with p.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OperationRow)) == 0


def test_enqueue_replay_and_legacy_endpoint_cannot_steal_queued_work(system, queued, monkeypatch):
    client, p, _ = system
    _, _, run = make(system)
    first = enqueue(client, run)
    assert enqueue(client, run)['revision'] == first['revision']
    monkeypatch.setattr(CandidateAgent, 'from_env', staticmethod(lambda: queued))
    r = client.post(f"/api/v1/candidate-runs/{run['id']}/process", headers=BUYER,
        json={'expected_revision': first['revision'], 'acknowledge_external_model': True})
    assert r.status_code == 200 and r.json()['status'] == 'QUEUED' and queued.calls == 0
    drain_once(CandidateService(p), factory=lambda: queued)
    assert enqueue(client, run)['status'] == 'NEEDS_REVIEW' and queued.calls == 1


def test_concurrent_workers_invoke_model_once(system, queued):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(drain_once, CandidateService(p), factory=lambda: queued) for _ in range(6)]
        for f in futures:
            f.result(timeout=20)
    assert queued.calls == 1 and stored(p, run)[0] == 'NEEDS_REVIEW'


@pytest.mark.parametrize('when', ['before', 'during'])
def test_cancellation_prevents_call_or_discards_late_result(system, queued, when):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run)
    def cancel(*_):
        current = client.get(f"/api/v1/candidate-runs/{run['id']}", headers=BUYER).json()
        response = client.post(f"/api/v1/candidate-runs/{run['id']}/cancel", headers=BUYER,
                               json={'expected_revision': current['revision']})
        assert response.status_code == 200
    if when == 'before': cancel()
    else: queued.effect = cancel
    drain_once(CandidateService(p), factory=lambda: queued)
    assert stored(p, run)[:2] == ('CANCELLED', None)
    assert queued.calls == (0 if when == 'before' else 1)
    assert drain_once(CandidateService(p), factory=lambda: queued) == []


@pytest.mark.parametrize('change', ['policy', 'expired', 'source', 'binding', 'actor', 'mode'])
def test_worker_revalidates_bound_inputs_and_authority(system, queued, change):
    client, p, _ = system
    req, doc, run = make(system)
    enqueue(client, run)
    if change == 'policy': publish_policy(client, budget_cap='25000.00')
    elif change == 'source':
        from procureflow.db import DocumentRow
        with p.db.transaction() as session:
            key = session.get(DocumentRow, doc['id']).storage_key
        (p.document_dir / key).write_text('changed source')
    elif change == 'actor': p.settings.auth_tokens.clear()
    elif change == 'mode': object.__setattr__(p.settings, 'mode', 'private')  # simulate an operator restart/config drift
    else:
        with p.db.transaction(write=True) as session:
            row = session.get(QuoteCandidateRunRow, run['id'])
            if change == 'expired': row.expires_at = '2000-01-01T00:00:00+00:00'
            else: row.dispatch_authority = {**row.dispatch_authority, 'user_id': 'forged'}
    drain_once(CandidateService(p), factory=lambda: queued)
    assert queued.calls == 0
    assert stored(p, run)[0] in {'STALE', 'FAILED'} and stored(p, run)[1] is None


def test_expired_running_is_interrupted_not_replayed(system, queued):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run)
    with p.db.transaction(write=True) as session:
        row = session.get(QuoteCandidateRunRow, run['id'])
        row.status, row.lease_until = 'RUNNING', '2000-01-01T00:00:00+00:00'
    drain_once(CandidateService(p), factory=lambda: queued)
    assert queued.calls == 0 and stored(p, run)[0] == 'INTERRUPTED'
    assert drain_once(CandidateService(p), factory=lambda: queued) == []


def test_active_leases_do_not_starve_queued_work(system, queued):
    client, p, _ = system
    req, _, first = make(system)
    enqueue(client, first)
    second = reserve(client, req['id'], source(client, req['id'], TEXT+'\nsecond source'), key='queue-second')
    enqueue(client, second)
    with p.db.transaction(write=True) as session:
        row = session.get(QuoteCandidateRunRow, first['id'])
        row.status = 'RUNNING'
        row.lease_until = (datetime.now(timezone.utc)+timedelta(seconds=80)).isoformat()
    result = drain_once(CandidateService(p), factory=lambda: queued, limit=1)
    assert result == [{'id': second['id'], 'status': 'NEEDS_REVIEW'}] and queued.calls == 1


def test_dispatch_revocation_during_call_discards_output(system, queued):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run)
    queued.effect = lambda *_: p.settings.auth_tokens.clear()
    drain_once(CandidateService(p), factory=lambda: queued)
    assert stored(p, run)[:2] == ('FAILED', None)


@pytest.mark.parametrize('headers,code', [(OTHER,404), (APPROVER,403), (AUDITOR,403), ({},401)])
def test_queue_scope_and_role_denial(system, queued, headers, code):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run, headers=headers, expected=code)
    assert stored(p, run)[0] == 'PENDING' and queued.calls == 0


@pytest.mark.parametrize('ack', [False, 1, 'true', None])
def test_queue_requires_explicit_boolean_ack(system, queued, ack):
    client, p, _ = system
    _, _, run = make(system)
    enqueue(client, run, expected=422, acknowledge_external_model=ack)
    assert stored(p, run)[0] == 'PENDING'


def test_queue_optin_and_worker_configuration_default_closed(system, monkeypatch):
    client, p, _ = system
    _, _, run = make(system)
    monkeypatch.delenv('PF_ALLOW_CANDIDATE_QUEUE', raising=False)
    enqueue(client, run, expected=503)
    monkeypatch.delenv('PF_ALLOW_CANDIDATE_MODEL', raising=False)
    with pytest.raises(DomainError, match='Explicit provider'):
        drain_once(CandidateService(p))
    assert stored(p, run)[0] == 'PENDING'


def test_queue_cap_does_not_modify_sixth_run(system, queued):
    client, p, _ = system
    req, _, first = make(system)
    enqueue(client, first)
    for i in range(1,6):
        doc = source(client, req['id'], TEXT+f'\nrevision {i}')
        run = reserve(client, req['id'], doc, key=f'queue-limit-{i}')
        enqueue(client, run, expected=202 if i<5 else 429)
    assert stored(p, run)[0] == 'PENDING'


def test_queue_binding_contains_no_tokens_and_is_not_exposed(system, queued):
    client, p, _ = system
    _, _, run = make(system)
    response = enqueue(client, run)
    authority = stored(p, run)[2]
    assert not any(secret in json.dumps(authority) for secret in p.settings.auth_tokens)
    assert authority['session_id'] is None
    assert not any(key in response for key in ('dispatch_authority', 'binding_hash', 'session_id', 'auth_version'))


@pytest.mark.parametrize('limit', [True,0,-1,101,'20'])
def test_worker_batch_bounds(limit):
    with pytest.raises(ValueError): pending_query(limit)


@pytest.mark.parametrize('change', ['revoke', 'expire', 'role'])
def test_pilot_queue_rechecks_original_session_before_network(tmp_path, monkeypatch, change):
    from procureflow.config import Settings
    from procureflow.db import Database, PilotSessionRow
    from procureflow.erp import MockERP
    from procureflow.service import ProcurementService
    from procureflow.contracts import RequestCreate
    from procureflow.candidate_contracts import CandidateReserve, CandidateProcess
    monkeypatch.setenv('PF_ALLOW_CANDIDATE_QUEUE', '1')
    db = Database(f'sqlite:///{tmp_path}/pilot.sqlite3', create_schema=True)
    settings = Settings(data_dir=tmp_path, database_url=db.engine.url.render_as_string(hide_password=False), mode='pilot')
    p = ProcurementService(db,settings,MockERP(tmp_path/'erp.sqlite3'))
    identities = p.identities
    identities.create_tenant('queue-pilot'); identities.set_membership('queue-pilot','buyer','buyer')
    login = identities.login(identities.issue_invite('queue-pilot','buyer')['credential'])
    principal = identities.authenticate(login['token'])
    req = p.create_request(principal, RequestCreate(title='Queue pilot',sku='STAND-01',quantity='20',budget='3000',max_delivery_days=14))
    service = CandidateService(p)
    doc = service.upload(principal, req['id'], 'source.txt', TEXT.encode())
    run = service.reserve(principal,req['id'],CandidateReserve(document_id=doc['id'],expected_version=req['version'],idempotency_key='queue-pilot-1'))
    service.enqueue(principal,run['id'],CandidateProcess(expected_revision=1,acknowledge_external_model=True))
    if change == 'revoke': identities.revoke_session(principal.session_id)
    elif change == 'role': identities.set_membership('queue-pilot','buyer','auditor')
    else:
        with db.transaction(write=True) as session:
            session.get(PilotSessionRow,principal.session_id).expires_at='2000-01-01T00:00:00+00:00'
    agent = FakeCandidate()
    drain_once(service,factory=lambda:agent)
    assert agent.calls==0 and stored(p,run)[:2]==('FAILED',None)
    db.engine.dispose()
