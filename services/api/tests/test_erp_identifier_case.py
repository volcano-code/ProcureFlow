"""Opaque ERP identifiers keep their case; unit/currency codes still normalize.

HTTP workflow tests here use MockERP. The separate Docker gate exercises the
mixed-case synthetic supplier against actual ERPNext and an independent Worker.
"""
import pytest
from conftest import BUYER, APPROVER, AUDITOR
from procureflow.contracts import QuoteValues
from procureflow.domain import digest
from procureflow.parsers import parse_document
from test_erp_verification import change_remote


@pytest.mark.parametrize('supplier,sku', [
    ('PF Synthetic Supplier', 'Item-aB-01'), ('Supplier-a', 'sku-b'),
    ('Straße GmbH', 'Part-ß'), ('供应商Mixed9', '产品aB-01'),
])
def test_model_preserves_opaque_identifier_case(supplier, sku):
    values = QuoteValues(supplier_id=supplier, sku=sku)
    assert values.supplier_id == supplier and values.sku == sku
    assert QuoteValues.model_validate(values.model_dump()).model_dump() == values.model_dump()


def test_currency_and_uom_normalization_is_not_removed():
    values = QuoteValues(supplier_id='Supplier-a', sku='Item-a', uom='ea', currency='cny')
    assert (values.uom, values.currency) == ('EA', 'CNY')
    assert QuoteValues().supplier_id is None and QuoteValues().sku is None


@pytest.mark.parametrize('extension,separator', [('txt', ': '), ('csv', ',')])
def test_text_extract_keeps_identifiers_equal_to_evidence(extension, separator):
    document = f'supplier_id{separator}PF Synthetic Supplier\nsku{separator}Item-aB-01\n'.encode()
    result = parse_document('quote.' + extension, document, 'doc')
    assert result['values']['supplier_id'] == 'PF Synthetic Supplier'
    assert result['values']['sku'] == 'Item-aB-01'
    assert 'PF Synthetic Supplier' in result['evidence']['supplier_id']['text']


def ready_mixed(system, monkeypatch):
    client, _, erp = system
    supplier, sku = 'PF Synthetic Supplier', 'Item-aB-01'
    monkeypatch.setattr(erp, 'suppliers', lambda: [{'name': supplier, 'supplier_name': supplier}])
    response = client.post('/api/v1/requests', headers=BUYER, json={
        'title': 'Mixed-case identifiers', 'sku': sku, 'quantity': '20',
        'budget': '3000.00', 'max_delivery_days': 14})
    assert response.status_code == 201
    req = response.json()
    values = {'supplier_id': supplier, 'sku': sku, 'quantity': '20', 'uom': 'EA',
        'unit_price': '100.00', 'tax_mode': 'excluded', 'tax_rate': '0',
        'shipping_cost': '0', 'discount': '0', 'delivery_days': 7, 'currency': 'CNY'}
    response = client.post(f"/api/v1/requests/{req['id']}/documents", headers=BUYER,
        files={'file': ('case.txt', '\n'.join(f'{k}: {v}' for k, v in values.items()).encode())})
    assert response.status_code == 201
    quote = response.json()
    assert quote['values']['supplier_id'] == supplier and quote['values']['sku'] == sku
    assert client.post(f"/api/v1/quotes/{quote['id']}/confirm", headers=BUYER,
        json={'expected_version': quote['version'], 'acknowledge': True}).status_code == 200
    proposal = client.post(f"/api/v1/requests/{req['id']}/analyze", headers=BUYER, json={}).json()['proposal']
    assert proposal and proposal['total'] == '2000.00'
    assert client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
        json={'snapshot_hash': proposal['snapshot_hash']}).status_code == 200
    return req, quote, proposal


@pytest.mark.parametrize('lose', [False, True])
def test_mixed_case_survives_approval_execution_recovery_and_readback(system, monkeypatch, lose):
    client, _, erp = system
    req, quote, proposal = ready_mixed(system, monkeypatch)
    response = client.post(f"/api/v1/requests/{req['id']}/execute", headers=BUYER,
        json={'snapshot_hash': proposal['snapshot_hash']})
    assert response.status_code == 202
    op = response.json()
    if lose:
        erp.fail_after_commit_once.add(op['id'])
    result = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    if lose:
        assert result['status'] == 'RECONCILING'
        result = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    assert result['status'] == 'COMPLETED' and erp.count() == 1
    remote = erp.find(op['id'])
    assert remote['supplier_id'] == quote['values']['supplier_id']
    assert remote['sku'] == quote['values']['sku']
    assert client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR).json()['status'] == 'verified'
    # Case-only remote drift is still a mismatch; never compare identities casefolded.
    change_remote(erp, op['id'], supplier_id=remote['supplier_id'].upper())
    assert client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR).json()['status'] == 'mismatch'
    assert erp.count() == 1


def test_case_only_edit_invalidates_prior_approval_and_preserves_history(system, monkeypatch):
    client, _, erp = system
    req, quote, proposal = ready_mixed(system, monkeypatch)
    values = {**quote['values'], 'supplier_id': quote['values']['supplier_id'].upper()}
    edited = client.put(f"/api/v1/quotes/{quote['id']}", headers=BUYER, json={
        'expected_version': quote['version'], 'values': values, 'reason': 'Correct ERP identifier'})
    assert edited.status_code == 200
    assert client.post(f"/api/v1/requests/{req['id']}/execute", headers=BUYER,
        json={'snapshot_hash': proposal['snapshot_hash']}).status_code == 409
    assert erp.count() == 0
    history = client.get(f"/api/v1/quotes/{quote['id']}/versions", headers=BUYER).json()
    assert history[0]['values']['supplier_id'] == quote['values']['supplier_id']


@pytest.mark.parametrize('field', ['supplier_id', 'sku'])
def test_case_changes_produce_distinct_snapshot_inputs(field):
    original = QuoteValues(supplier_id='Supplier-a', sku='Item-a').model_dump(mode='json')
    changed = {**original, field: original[field].upper()}
    assert digest(original) != digest(QuoteValues.model_validate(changed).model_dump(mode='json'))
