"""Receipt and recovery tests. Stored COMPLETED status is not current ERP truth."""
import json
import pytest
from dataclasses import replace
from sqlalchemy import select
from conftest import approved, enqueue, BUYER, AUDITOR, OTHER
from procureflow.db import OperationRow
from procureflow.erp import ERPUnknown


def completed(system):
    client, service, erp = system
    req, _, proposal = approved(client)
    op = enqueue(client, req, proposal)
    result = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    assert result['status'] == 'COMPLETED'
    return op


def change_remote(erp, key, **changes):
    with erp._connect() as conn:
        record = json.loads(conn.execute('SELECT data FROM drafts WHERE operation_key=?', (key,)).fetchone()[0])
        record.update(changes)
        conn.execute('UPDATE drafts SET data=? WHERE operation_key=?', (json.dumps(record), key))


def test_readback_receipt_does_not_dispatch_or_change_operation(system, monkeypatch):
    client, service, erp = system
    op = completed(system)
    monkeypatch.setattr(erp, 'create_draft', lambda *_: pytest.fail('verification attempted a write'))
    for headers in (BUYER, AUDITOR):
        response = client.post(f"/api/v1/operations/{op['id']}/verify", headers=headers)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt['status'] == 'verified' and receipt['draft_verified']
        assert receipt['simulated'] is True and receipt['external_write_attempted'] is False
    assert erp.count() == 1
    assert client.get(f"/api/v1/operations/{op['id']}", headers=BUYER).json()['attempts'] == 1


@pytest.mark.parametrize('changes', [
    {'unit_price': '1'}, {'transaction_date': '1999-01-01'}, {'operation_key': 'wrong'},
    {'name': 'another-id'}, {'docstatus': 1}, {'unit_price': None}, {'total': 'NaN'},
])
def test_receipt_detects_remote_drift_but_keeps_history(system, changes):
    client, _, erp = system
    op = completed(system)
    change_remote(erp, op['id'], **changes)
    receipt = client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR).json()
    assert receipt['status'] == 'mismatch' and receipt['draft_verified'] is False
    assert receipt['remote_id'] is None
    assert client.get(f"/api/v1/operations/{op['id']}", headers=BUYER).json()['status'] == 'COMPLETED'
    assert erp.count() == 1


@pytest.mark.parametrize('field,value', [('unit_price','1'), ('transaction_date','1999-01-01'), ('operation_key','wrong')])
def test_recovery_never_accepts_incomplete_snapshot_match(system, field, value):
    client, service, erp = system
    req, _, proposal = approved(client); op = enqueue(client, req, proposal)
    erp.fail_after_commit_once.add(op['id'])
    first = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    assert first['status'] == 'RECONCILING'
    change_remote(erp, op['id'], **{field:value})
    recovered = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    assert recovered['status'] == 'NEEDS_HUMAN' and recovered['error'] == 'REMOTE_PAYLOAD_MISMATCH'
    assert erp.count() == 1


def test_verification_denies_cross_tenant_and_unauthenticated(system):
    client, _, erp = system; op = completed(system)
    assert client.post(f"/api/v1/operations/{op['id']}/verify", headers=OTHER).status_code == 404
    assert client.post(f"/api/v1/operations/{op['id']}/verify").status_code == 401


@pytest.mark.parametrize('case', ['missing','outage','changed_target','changed_payload'])
def test_verification_unknown_or_changed_target_is_not_success(system, monkeypatch, case):
    client, service, erp = system; op = completed(system)
    if case == 'missing':
        monkeypatch.setattr(erp, 'find', lambda *_: None)
    elif case == 'outage':
        def fail(*_): raise ERPUnknown('SECRET_PRIVATE_ERROR_DO_NOT_LEAK')
        monkeypatch.setattr(erp, 'find', fail)
    elif case == 'changed_target':
        service.settings = replace(service.settings, erp_url='https://other.example.invalid')
        monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('wrong target contacted'))
    else:
        with service.db.transaction(write=True) as session:
            row = session.get(OperationRow, op['id']); row.payload = {**row.payload, 'total':'1.00'}
        monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('corrupted payload used'))
    receipt = client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR)
    assert receipt.json()['status'] == {'missing':'missing','outage':'unavailable','changed_target':'blocked','changed_payload':'blocked'}[case]
    assert not receipt.json()['matches_snapshot']
    assert 'SECRET_PRIVATE_ERROR' not in receipt.text


def test_recovery_target_change_stops_before_network(system, monkeypatch):
    client, service, erp = system
    req, _, proposal = approved(client); op = enqueue(client, req, proposal)
    erp.fail_after_commit_once.add(op['id'])
    assert client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()['status'] == 'RECONCILING'
    service.settings = replace(service.settings, erp_url='https://other.example.invalid')
    monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('changed ERP contacted'))
    assert client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()['error'] == 'EXECUTION_TARGET_OR_SNAPSHOT_CHANGED'
    assert erp.count() == 1
