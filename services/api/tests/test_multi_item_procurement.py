"""Complete-supplier CNY/EA baskets, all synthetic and local."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from conftest import APPROVER, AUDITOR, BUYER, OTHER, enqueue, upload, publish_policy
from procureflow.contracts import Principal, QuoteValues, RequestCreate, TableImportPreview
from procureflow.db import ApprovalRow, OperationRow, RecoveryHoldRow, now
from procureflow.domain import calculate, offer_check
from procureflow.erp import ERPUnknown


def request_data():
    return {"title": "Two-item local basket", "lines": [{"sku": "STAND-01", "quantity": "2", "uom": "EA"},
             {"sku": "CABLE-02", "quantity": "3", "uom": "EA"}], "budget": "1000.00", "max_delivery_days": 14, "currency": "CNY"}


def quote_data():
    return {"supplier_id": "SUP-A", "currency": "CNY", "shipping_cost": "5.00", "lines": [
        {"sku": "STAND-01", "quantity": "2", "uom": "EA", "unit_price": "100.00", "tax_mode": "excluded",
         "tax_rate": "0.10", "discount": "20.00", "delivery_days": 7},
        {"sku": "CABLE-02", "quantity": "3", "uom": "EA", "unit_price": "10.00", "tax_mode": "included",
         "tax_rate": "0.13", "discount": "0.00", "delivery_days": 9}]}


def setup_multi(client, *, approve=True, values=None):
    response = client.post('/api/v1/requests', headers=BUYER, json=request_data())
    assert response.status_code == 201, response.text
    req = response.json()
    imported = upload(client, req['id'])
    response = client.put(f"/api/v1/quotes/{imported['id']}", headers=BUYER,
        json={"expected_version": 1, "values": values or quote_data(), "reason": "Explicit synthetic line correction"})
    assert response.status_code == 200, response.text
    quote = response.json()
    response = client.post(f"/api/v1/quotes/{quote['id']}/confirm", headers=BUYER,
        json={"expected_version": quote['version'], "acknowledge": True})
    assert response.status_code == 200, response.text
    analysis = client.post(f"/api/v1/requests/{req['id']}/analyze", headers=BUYER, json={}).json()
    proposal = analysis['proposal']
    if approve:
        assert proposal, analysis
        response = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
                               json={"snapshot_hash": proposal['snapshot_hash']})
        assert response.status_code == 200, response.text
    return req, quote, proposal


def test_per_line_rounding_discounts_tax_and_freight_once():
    result = calculate(QuoteValues.model_validate(quote_data()))
    assert result['total'] == '233.00'
    assert result['goods'] == '230.00' and result['discount'] == '20.00'
    assert result['added_tax'] == '18.00' and result['shipping'] == '5.00'
    assert [line['total'] for line in result['lines']] == ['198.00', '30.00']
    data = quote_data()
    for line in data['lines']:
        line.update(quantity='3', unit_price='0.05', tax_mode='excluded', tax_rate='0.1', discount='0')
    data['shipping_cost'] = '0'
    assert calculate(QuoteValues.model_validate(data))['total'] == '0.34'


@pytest.mark.parametrize('field', ['quantity', 'unit_price', 'discount', 'tax_rate'])
def test_incomplete_line_never_gets_dropped_from_total(field):
    data = quote_data(); data['lines'][0][field] = None
    result = offer_check(request_data(), data, True)
    assert result['total'] is None and not result['eligible']
    assert f'lines.0.{field}' in result['missing']


@pytest.mark.parametrize('field,value,issue', [('quantity','1','QUANTITY_MISMATCH'), ('uom','BOX','UOM_MISMATCH'),
    ('delivery_days',30,'DELIVERY_EXCEEDS_LIMIT'), ('sku',None,'MISSING_SKU')])
def test_each_line_checked_against_its_request(field, value, issue):
    data = quote_data(); data['lines'][1][field] = value
    result = offer_check(request_data(), data, True)
    assert not result['eligible'] and issue in result['lines'][1]['violations']


def test_partial_extra_and_unconfirmed_quotes_are_ineligible():
    data = quote_data(); data['lines'].pop()
    result = offer_check(request_data(), data, True)
    assert result['coverage']['missing_skus'] == ['CABLE-02']
    assert 'MISSING_REQUEST_LINES' in result['violations'] and not result['eligible']
    data['lines'][0]['sku'] = 'not-requested'
    result = offer_check(request_data(), data, True)
    assert 'UNEXPECTED_QUOTE_LINES' in result['violations']
    assert not offer_check(request_data(), quote_data(), False)['eligible']


def test_aggregate_budget_and_policy_delivery_cap():
    result = offer_check(request_data(), quote_data(), True, {'budget_cap':'232.99','max_delivery_days':8})
    assert result['total'] == '233.00'
    assert 'BUDGET_EXCEEDED' in result['violations'] and 'LINE_2_DELIVERY_EXCEEDS_LIMIT' in result['violations']


@pytest.mark.parametrize('model,body', [
    (RequestCreate, {**request_data(), 'lines': []}),
    (RequestCreate, {**request_data(), 'sku':'X', 'quantity':'1'}),
    (RequestCreate, {**request_data(), 'lines':[request_data()['lines'][0]] * 2}),
    (RequestCreate, {**request_data(), 'lines':[{'sku':str(i),'quantity':'1'} for i in range(21)]}),
    (QuoteValues, {**quote_data(), 'lines': []}),
    (QuoteValues, {**quote_data(), 'lines':[quote_data()['lines'][0]] * 2}),
    (QuoteValues, {**quote_data(), 'unit_price':'1'}),
    (QuoteValues, {**quote_data(), 'tax_mode':'included'}),
    (QuoteValues, {**quote_data(), 'lines':[{**quote_data()['lines'][0], 'quantity':2}]}),
])
def test_ambiguous_duplicate_or_unbounded_contract_rejected(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_opaque_case_sensitive_skus_and_legacy_shape_preserved():
    body = request_data(); body['lines'][1]['sku'] = 'stand-01'
    assert len(RequestCreate.model_validate(body).lines) == 2
    assert 'lines' not in RequestCreate(title='x',sku='x',quantity='1',budget='1').model_dump()
    assert 'lines' not in QuoteValues().model_dump()


@pytest.mark.parametrize('overrides', [{'row':2,'rows':[2,3]}, {'rows':[2,2]}, {'rows':[]}, {'rows':[True]}, {}])
def test_invalid_row_selection_contract(overrides):
    with pytest.raises(ValidationError):
        TableImportPreview.model_validate({'expected_revision':1,'sheet':'CSV','header_row':1,'mapping':{'sku':'A'},**overrides})


def test_whole_basket_approval_execution_replay_and_readonly_verification(system):
    client, service, erp = system
    req, quote, proposal = setup_multi(client)
    assert proposal['contract_version'] == 'multi-sku-v1' and proposal['total'] == '233.00'
    assert len(proposal['quote_values']['lines']) == 2
    op = enqueue(client, req, proposal)
    response = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER)
    assert response.json()['status'] == 'COMPLETED', response.text
    assert len(erp.find(op['id'])['lines']) == 2
    for _ in range(2):
        assert enqueue(client, req, proposal)['id'] == op['id']
        assert client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR).json()['status'] == 'verified'
    assert erp.count() == 1
    assert client.get(f"/api/v1/requests/{req['id']}", headers=OTHER).status_code == 404
    assert client.post(f"/api/v1/operations/{op['id']}/verify", headers=OTHER).status_code == 404


@pytest.mark.parametrize('change', ['request_line','quote_line','policy'])
def test_any_line_or_policy_change_stales_whole_approval(system, change):
    client, service, erp = system
    req, quote, proposal = setup_multi(client)
    if change == 'request_line':
        current = client.get(f"/api/v1/requests/{req['id']}", headers=BUYER).json()
        body = request_data(); body['lines'][1]['quantity'] = '4'
        response = client.put(f"/api/v1/requests/{req['id']}", headers=BUYER,
                              json={**body,'expected_version':current['version']})
    elif change == 'quote_line':
        data = quote_data(); data['lines'][1]['unit_price'] = '11.00'
        response = client.put(f"/api/v1/quotes/{quote['id']}",headers=BUYER,
            json={'expected_version':quote['version'],'values':data,'reason':'Explicit line correction'})
        assert response.json()['evidence']['lines.1.unit_price']['kind'] == 'manual'
        assert response.json()['confirmed_by'] is None
    else:
        publish_policy(client, budget_cap='900')
        response = client.get(f"/api/v1/requests/{req['id']}",headers=BUYER)
    assert response.status_code == 200, response.text
    assert client.post(f"/api/v1/requests/{req['id']}/execute",headers=BUYER,
                       json={'snapshot_hash':proposal['snapshot_hash']}).status_code == 409
    assert erp.count() == 0
    with service.db.transaction() as session:
        assert session.scalar(select(ApprovalRow)).status == 'STALE'


def test_partial_coverage_cannot_approve_or_execute(system):
    client, _, erp = system
    data = quote_data(); data['lines'].pop()
    req, _, proposal = setup_multi(client, approve=False, values=data)
    assert proposal is None and erp.count() == 0
    assert client.post(f"/api/v1/requests/{req['id']}/approval",headers=APPROVER,
        json={'snapshot_hash':'0'*64}).status_code == 409


def test_lost_response_and_uncertain_absence_never_duplicate_basket(system, monkeypatch):
    client, _, erp = system
    req, _, proposal = setup_multi(client); op = enqueue(client, req, proposal)
    erp.fail_after_commit_once.add(op['id'])
    assert client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()['status'] == 'RECONCILING'
    assert erp.count() == 1
    assert client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()['status'] == 'COMPLETED'
    req, _, proposal = setup_multi(client); op = enqueue(client, req, proposal); calls = []
    def uncertain(*args):
        calls.append(1); raise ERPUnknown('synthetic uncertainty')
    monkeypatch.setattr(erp,'create_draft',uncertain)
    assert client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()['status'] == 'RECONCILING'
    assert client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()['status'] == 'NEEDS_HUMAN'
    assert len(calls) == 1 and erp.count() == 1


def test_held_basket_reconciliation_never_releases_or_replays(system, monkeypatch):
    from test_recovery_diagnostics import snapshot
    client, service, erp = system
    req, _, proposal = setup_multi(client); op = enqueue(client,req,proposal)
    remote = erp.create_draft(op['id'], proposal)
    with service.db.transaction(write=True) as session:
        session.add(RecoveryHoldRow(operation_id=op['id'],restore_id='multi-synthetic',original_status='IN_FLIGHT',created_at=now()))
    before = snapshot(service.db)
    receipt = client.get(f"/api/v1/recovery/operations/{op['id']}/reconciliation",headers=AUDITOR).json()
    assert receipt['status'] == 'verified' and receipt['differences'] == [], receipt
    assert len(receipt['expected']['lines']) == 2 and not receipt['replay_permitted']
    assert snapshot(service.db) == before
    remote['lines'][0]['quantity'] = '999'
    monkeypatch.setattr(erp,'find',lambda _: remote)
    mismatch = client.get(f"/api/v1/recovery/operations/{op['id']}/reconciliation",headers=AUDITOR).json()
    assert mismatch['status'] == 'mismatch'
    assert any(d['field'].startswith('lines[') for d in mismatch['differences'])
    blocked = client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER)
    assert blocked.json()['error']['code'] == 'RECOVERY_OPERATION_HELD'
    assert snapshot(service.db) == before and erp.count() == 1


def test_line_identity_replacement_cannot_borrow_unchanged_value_evidence(system):
    client, service, _ = system
    req, quote, _ = setup_multi(client)
    from procureflow.db import QuoteVersionRow
    with service.db.transaction(write=True) as session:
        version = session.scalar(select(QuoteVersionRow).where(QuoteVersionRow.quote_id == quote['id'],
                                                               QuoteVersionRow.version == quote['version']))
        version.evidence = {**version.evidence, 'lines.0.unit_price': {'kind':'source','fragment_id':'old-item-price'}}
    data = quote_data(); data['lines'][0]['sku'] = 'REPLACEMENT-ITEM'
    response = client.put(f"/api/v1/quotes/{quote['id']}",headers=BUYER,
        json={'expected_version':quote['version'],'values':data,'reason':'Replace item identity explicitly'})
    assert response.status_code == 200, response.text
    for field in data['lines'][0]:
        assert response.json()['evidence'][f'lines.0.{field}']['kind'] == 'manual'
    assert response.json()['confirmed_by'] is None


def test_recovery_multi_cost_decimal_formats_and_optional_inclusive_rate(system):
    from procureflow.recovery import comparison
    client, _, erp = system
    data = quote_data(); data['lines'][1]['tax_rate'] = None
    req, _, proposal = setup_multi(client, values=data)
    op = enqueue(client, req, proposal)
    remote = erp.create_draft(op['id'], proposal)
    costs = remote['cost_values']['calculated']
    for field in ('goods', 'added_tax', 'shipping'):
        costs[field] += '0'
        for line in costs['lines']:
            line[field] += '0'
    expected, observed, differences = comparison(remote, proposal, op['id'], remote['name'])
    assert differences == []
    from procureflow.erp import remote_matches
    assert remote_matches(remote, proposal, op['id'])
