"""Offline, exact-decimal adapter contracts; live evidence belongs to ERP CI."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

from procureflow.erp import ERPNextClient, ERPRejected, ERPUnknown, cost_mapping, remote_matches
from conftest import approved, enqueue, BUYER, AUDITOR

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'integrations/erpnext/sandbox'))
from cost_fixtures import COST_CASES, TAX_ACCOUNT, FREIGHT_ACCOUNT


def payload(scenario='normal'):
    fixture = COST_CASES[scenario]
    return {'snapshot_hash': 'a'*64, 'erp_company': 'Demo', 'transaction_date': '2026-10-01',
        'erp_cost_accounts': {'tax': TAX_ACCOUNT, 'freight': FREIGHT_ACCOUNT}, 'total': fixture['total'],
        'quote_values': {'supplier_id': 'Synthetic Supplier', 'sku': 'ITEM', 'quantity': '20',
            'uom': 'EA', 'currency': 'CNY', **{key: fixture[key] for key in
                ('unit_price', 'tax_mode', 'tax_rate', 'shipping_cost', 'discount')}}}


def persisted(body, scenario):
    """Independent fixture expectations, not cost_mapping's calculated proof."""
    fixture = COST_CASES[scenario]
    record = deepcopy(body)
    record.update(name='SQ-COST', total=fixture['goods'], net_total=fixture['net'],
        grand_total=fixture['total'], total_taxes_and_charges=str(Decimal(fixture['tax_after']) + Decimal(fixture['shipping_cost'])))
    record['items'][0].update(amount=fixture['goods'], net_amount=fixture['net'],
        net_rate=str((Decimal(fixture['net'])/20).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')))
    tax, freight = record['taxes']
    tax.update(tax_amount=fixture['tax_before'], tax_amount_after_discount_amount=fixture['tax_after'],
        total=str(Decimal(fixture['net'])+Decimal(fixture['tax_after'])))
    freight.update(rate=None, tax_amount_after_discount_amount=fixture['shipping_cost'], total=fixture['total'])
    return record


def endpoint(scenario='normal', mutation=None, post_status=200):
    calls, state = [], {}
    def handle(request):
        calls.append(request)
        if '/Company/' in request.url.path:
            return httpx.Response(200, json={'data': {'name': 'Demo', 'default_currency': 'CNY'}})
        if request.url.path.endswith('/Custom Field'):
            field = json.loads(request.url.params['filters'])[-1][-1]
            return httpx.Response(200, json={'data': [{'fieldname': field, 'unique': 1, 'fieldtype': 'Data'}]})
        if request.method == 'POST':
            body = json.loads(request.content)
            state.update(persisted(body, scenario))
            return httpx.Response(post_status, json={'data': state})
        if request.url.path.endswith('/Supplier Quotation'):
            return httpx.Response(200, json={'data': [{'name': 'SQ-COST'}] if state else []})
        if request.url.path.endswith('/SQ-COST'):
            result = deepcopy(state)
            if mutation: mutation(result)
            return httpx.Response(200, json={'data': result})
        pytest.fail(f'Unexpected endpoint: {request.method} {request.url.path}')
    adapter = ERPNextClient('https://erp.test', 'test-key', 'test-secret', 'Demo', True,
        transport=httpx.MockTransport(handle), tax_account=TAX_ACCOUNT, freight_account=FREIGHT_ACCOUNT)
    return adapter, calls, state


@pytest.mark.parametrize('scenario', COST_CASES)
def test_four_independent_cost_fixture_roundtrips(scenario):
    p = payload(scenario)
    adapter, calls, _ = endpoint(scenario)
    try:
        result = adapter.create_draft('cost-op', p)
        assert remote_matches(result, p, 'cost-op')
        assert result['total'] == COST_CASES[scenario]['total']
        assert calls[-1].method == 'GET'
        assert sum(c.method == 'POST' for c in calls) == 1
        assert all(c.method in {'GET', 'POST'} for c in calls)
        assert result['cost_proof']['taxes'][1]['tax_amount_after_discount_amount'] == COST_CASES[scenario]['shipping_cost']
    finally: adapter.client.close()


MUTATIONS = [
    lambda r: r['taxes'][0].update(rate='12'),
    lambda r: r['taxes'][0].update(included_in_print_rate=0),
    lambda r: r['taxes'][0].update(account_head='wrong-account'),
    lambda r: r['taxes'][0].update(category='Valuation and Total'),
    lambda r: r['taxes'][0].update(add_deduct_tax='Deduct'),
    lambda r: r['taxes'][0].update(charge_type='On Previous Row Total'),
    lambda r: r['taxes'][0].update(tax_amount='1'),
    lambda r: r['taxes'][0].update(tax_amount_after_discount_amount='1'),
    lambda r: r['taxes'][0].update(dont_recompute_tax=1),
    lambda r: r['taxes'][0].update(row_id='1'),
    lambda r: r['taxes'][0].update(total='1'),
    lambda r: r['taxes'][1].update(tax_amount='801'),
    lambda r: r['taxes'][1].update(tax_amount_after_discount_amount='801'),
    lambda r: r['taxes'][1].update(rate='1'),
    lambda r: r['taxes'].reverse(),
    lambda r: r['taxes'].append(deepcopy(r['taxes'][1])),
    lambda r: r.update(taxes=None),
    lambda r: r.update(discount_amount='1'),
    lambda r: r.update(additional_discount_percentage='1'),
    lambda r: r.update(apply_discount_on='Net Total'),
    lambda r: r.update(net_total='1'),
    lambda r: r.update(total_taxes_and_charges='1'),
    lambda r: r.update(conversion_rate='2'),
    lambda r: r.update(disable_rounded_total=0),
    lambda r: r.update(taxes_and_charges='Template'),
    lambda r: r.update(shipping_rule='Rule'),
    lambda r: r.update(tax_category='Category'),
    lambda r: r['items'][0].update(item_tax_template='Item Template'),
    lambda r: r['items'][0].update(discount_amount='1'),
    lambda r: r['items'][0].update(discount_percentage='1'),
    lambda r: r['items'][0].update(price_list_rate='1'),
    lambda r: r['items'][0].update(net_rate='1'),
    lambda r: r['items'][0].update(amount='1'),
    lambda r: r['items'][0].update(net_amount='1'),
    # Same final total, offsetting component tampering must still fail.
    lambda r: (r['taxes'][0].update(tax_amount_after_discount_amount='2760.06'),
               r['taxes'][1].update(tax_amount_after_discount_amount='801.00')),
]


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_cost_row_drift_rejected_despite_correct_post_echo_and_grand_total(mutation):
    adapter, calls, _ = endpoint(mutation=mutation)
    try:
        with pytest.raises(ERPRejected): adapter.create_draft('cost-op', payload())
        assert sum(c.method == 'POST' for c in calls) == 1
    finally: adapter.client.close()


@pytest.mark.parametrize('field,value', [('tax_rate', None), ('tax_mode', 'unknown'),
    ('shipping_cost', None), ('discount', None), ('shipping_cost', '-1'), ('discount', '24001'),
    ('tax_rate', '1.01'), ('tax_rate', 'NaN'), ('unit_price', 'Infinity'),
    ('unit_price', 1200.0), ('shipping_cost', '0.001'), ('currency', 'USD'), ('uom', 'BOX')])
def test_unrepresentable_payload_fails_before_network(field, value):
    p = payload(); p['quote_values'][field] = value
    adapter, calls, _ = endpoint()
    try:
        with pytest.raises(ERPRejected): adapter.create_draft('cost-op', p)
        assert not calls
    finally: adapter.client.close()


@pytest.mark.parametrize('mutation', [lambda p: p.update(total='24800.01'),
    lambda p: p['erp_cost_accounts'].update(tax='wrong-account'),
    lambda p: p['erp_cost_accounts'].update(freight=''),
    lambda p: p.update(erp_company='Other')])
def test_total_target_and_account_snapshot_are_bound_before_network(mutation):
    p = payload(); mutation(p)
    adapter, calls, _ = endpoint()
    try:
        with pytest.raises(ERPRejected): adapter.create_draft('cost-op', p)
        assert not calls
    finally: adapter.client.close()


def test_matching_duplicate_key_can_reconcile_but_cost_drift_cannot():
    for mutation in (None, lambda r: r['taxes'][1].update(tax_amount='1')):
        adapter, calls, _ = endpoint(post_status=409, mutation=mutation)
        try:
            if mutation:
                with pytest.raises(ERPRejected): adapter.create_draft('cost-op', payload())
            else:
                assert adapter.create_draft('cost-op', payload())['name'] == 'SQ-COST'
            assert sum(c.method == 'POST' for c in calls) == 1
        finally: adapter.client.close()


def test_lost_receipt_recovers_by_independent_read_and_retains_components():
    adapter, calls, state = endpoint(post_status=504)
    try:
        with pytest.raises(ERPUnknown): adapter.create_draft('cost-op', payload())
        assert remote_matches(adapter.find('cost-op'), payload(), 'cost-op')
        state['taxes'][1]['tax_amount_after_discount_amount'] = '1'
        assert not remote_matches(adapter.find('cost-op'), payload(), 'cost-op')
        assert sum(c.method == 'POST' for c in calls) == 1
    finally: adapter.client.close()


@pytest.mark.parametrize('mode,unit,discount,total', [
    ('included','0.05','0.01','0.04'), # 0.05 / 1.13 rounds then discount distribution loses a penny
    ('included','0.01','0','0.01'), # half-cent inclusive split at 100% tax
])
def test_inclusive_rounding_redistribution_is_rejected(mode, unit, discount, total):
    p=payload();p['quote_values'].update(quantity='1', unit_price=unit, discount=discount,
        shipping_cost='0', tax_mode=mode, tax_rate='1' if unit=='0.01' else '0.13');p['total']=total
    with pytest.raises(ERPRejected, match='INCLUSIVE_ROUNDING_UNSUPPORTED'): cost_mapping(p)


def test_no_discount_inclusive_tax_uses_unrounded_base():
    p=payload();p['quote_values'].update(quantity='1', unit_price='0.04', tax_rate='0.14', shipping_cost='0');p['total']='0.04'
    _, proof=cost_mapping(p)
    assert proof['net_total']=='0.04' and proof['taxes'][0]['tax_amount_after_discount_amount']=='0.00'


@pytest.mark.parametrize('setting', ['erp_tax_account', 'erp_freight_account'])
def test_account_configuration_change_invalidates_approval_and_recovery(system, monkeypatch, setting):
    client, service, erp = system
    req, _, proposal = approved(client); op = enqueue(client, req, proposal)
    service.settings = replace(service.settings, **{setting: 'Changed account'})
    monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('changed accounting configuration reached ERP'))
    result = client.post(f"/api/v1/operations/{op['id']}/process", headers=BUYER).json()
    assert result['status'] == 'NEEDS_HUMAN' and result['error'] == 'EXECUTION_TARGET_OR_SNAPSHOT_CHANGED'
    assert erp.count() == 0


@pytest.mark.parametrize('values', [
    {'quantity':'389.825', 'unit_price':'70681.40'},
    {'quantity':'2', 'unit_price':'500000.01'},
    {'quantity':'1', 'unit_price':'1000000.01'},
    {'quantity':'20', 'unit_price':'1200', 'shipping_cost':'1000000.01'},
])
def test_numeric_mapping_bounds_fail_before_network(values):
    p=payload();p['quote_values'].update(values)
    adapter, calls, _=endpoint()
    try:
        with pytest.raises(ERPRejected, match='BOUNDS_EXCEEDED'): adapter.create_draft('cost-op', p)
        assert not calls
    finally: adapter.client.close()


@pytest.mark.parametrize('field', ['pricing_rules', 'item_pricing_rules'])
def test_pricing_rules_fail_closed_even_without_monetary_drift(field):
    def change(record):
        (record['items'][0] if field == 'item_pricing_rules' else record)['pricing_rules'] = 'Unexpected Rule'
    adapter, _, _=endpoint(mutation=change)
    try:
        with pytest.raises(ERPRejected): adapter.create_draft('cost-op', payload())
    finally: adapter.client.close()


@pytest.mark.parametrize('scenario', COST_CASES)
def test_independent_database_audit_checks_exact_components(scenario):
    from cost_audit import verify_cost_document
    p=payload(scenario); adapter, _, state=endpoint(scenario)
    try: adapter.create_draft('cost-op', p)
    finally: adapter.client.close()
    state['company']='ProcureFlow Sandbox';state['items'][0]['item_code']='PF-SANDBOX-ITEM'
    verify_cost_document(state, COST_CASES[scenario])
    for change in MUTATIONS:
        altered=deepcopy(state);change(altered)
        # Mutations changing to existing values in this scenario are not drift.
        if altered == state:continue
        with pytest.raises((AssertionError, TypeError, KeyError, ValueError)):
            verify_cost_document(altered, COST_CASES[scenario])


@pytest.mark.parametrize('missing', ['erp_cost_mapping_version', 'erp_cost_accounts'])
def test_legacy_frozen_snapshot_is_blocked_without_rewriting_history(system, monkeypatch, missing):
    from sqlalchemy import select
    from procureflow.db import OperationRow
    from procureflow.domain import digest
    client, service, erp=system
    req, _, proposal=approved(client); op=enqueue(client, req, proposal)
    with service.db.transaction(write=True) as session:
        row=session.get(OperationRow, op['id']); old=dict(row.payload);old.pop(missing)
        old['snapshot_hash']=digest({key:value for key,value in old.items() if key!='snapshot_hash'})
        row.payload=old
    monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('legacy target contacted ERP'))
    receipt=client.post(f"/api/v1/operations/{op['id']}/verify", headers=AUDITOR).json()
    assert receipt['status']=='blocked'
    with service.db.transaction() as session:
        assert missing not in session.get(OperationRow, op['id']).payload
    assert erp.count()==0


@pytest.mark.parametrize('value', [False, 0, [], {}])
@pytest.mark.parametrize('field', ['row_id', 'taxes_and_charges', 'shipping_rule', 'tax_category', 'item_tax_template', 'item_pricing_rules'])
def test_malformed_falsy_optional_fields_are_not_silently_blank(field, value):
    def change(record):
        if field == 'row_id': record['taxes'][0][field] = value
        elif field in {'item_tax_template', 'item_pricing_rules'}:
            record['items'][0]['pricing_rules' if field == 'item_pricing_rules' else field] = value
        else: record[field] = value
    adapter, _, _ = endpoint(mutation=change)
    try:
        with pytest.raises(ERPRejected): adapter.create_draft('cost-op', payload())
    finally: adapter.client.close()


def test_missing_simulation_marker_cannot_skip_component_verification():
    adapter, _, _=endpoint()
    try: result=adapter.create_draft('cost-op', payload())
    finally: adapter.client.close()
    result.pop('simulated');result.pop('cost_proof')
    assert not remote_matches(result, payload(), 'cost-op')


@pytest.mark.parametrize('mode,rate,goods,discount,freight,total,net,tax', [
    ('excluded','0','200','10','5','195','190','0'),
    ('included','0','200','10','5','195','190','0'),
    ('excluded','0.13','200','200','5','5','0','0'),
    ('included','0.13','226','226','5','5','0','0'),
    ('excluded','0.1','0.05','0','0','0.06','0.05','0.01'),
    ('included','0.13','0','0','5','5','0','0'),
    ('excluded','0','1000000','0','0','1000000','1000000','0'),
])
def test_exact_decimal_zero_full_discount_rounding_and_upper_bound(mode, rate, goods, discount, freight, total, net, tax):
    p=payload();p['total']=total
    p['quote_values'].update(quantity='1', unit_price=goods, tax_mode=mode,
        tax_rate=rate, discount=discount, shipping_cost=freight)
    body, proof=cost_mapping(p)
    assert Decimal(proof['net_total'])==Decimal(net)
    assert Decimal(proof['total_taxes_and_charges'])==Decimal(tax)+Decimal(freight)
    assert Decimal(proof['net_total'])+Decimal(proof['total_taxes_and_charges'])==Decimal(total)
    assert len(body['taxes']) == int(Decimal(rate)>0)+int(Decimal(freight)>0)


@pytest.mark.parametrize('setting', ['erp_tax_account', 'erp_freight_account'])
def test_account_change_blocks_lost_receipt_recovery_without_second_write(system, monkeypatch, setting):
    client, service, erp=system
    req, _, proposal=approved(client);op=enqueue(client, req, proposal)
    erp.fail_after_commit_once.add(op['id'])
    assert client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()['status']=='RECONCILING'
    service.settings=replace(service.settings, **{setting:'Changed'})
    monkeypatch.setattr(erp, 'find', lambda *_: pytest.fail('changed accounting target queried'))
    result=client.post(f"/api/v1/operations/{op['id']}/process",headers=BUYER).json()
    assert result['status']=='NEEDS_HUMAN' and result['error']=='EXECUTION_TARGET_OR_SNAPSHOT_CHANGED'
    assert erp.count()==1


@pytest.mark.parametrize('currency', ['USD', None, '', 'cny', False])
@pytest.mark.parametrize('action', ['create', 'find', 'preflight'])
def test_non_cny_or_unverified_company_currency_fails_before_business_access(currency, action):
    calls=[]
    def handle(request):
        calls.append(request)
        if '/Company/' in request.url.path:
            return httpx.Response(200,json={'data':{'name':'Demo','default_currency':currency}})
        if request.url.path.endswith('/Custom Field'):
            field=json.loads(request.url.params['filters'])[-1][-1]
            return httpx.Response(200,json={'data':[{'fieldname':field,'unique':1,'fieldtype':'Data'}]})
        pytest.fail('Unsupported company currency reached business endpoint')
    adapter=ERPNextClient('https://erp.test','k','s','Demo',True,transport=httpx.MockTransport(handle),
        tax_account=TAX_ACCOUNT,freight_account=FREIGHT_ACCOUNT)
    try:
        with pytest.raises(ERPRejected,match='COMPANY_CURRENCY_NOT_SUPPORTED'):
            if action=='create':adapter.create_draft('op',payload())
            elif action=='find':adapter.find('op')
            else:adapter.preflight()
        assert all(request.method=='GET' for request in calls)
    finally:adapter.client.close()


@pytest.mark.parametrize('scenario', COST_CASES)
def test_wire_uses_numeric_tokens_before_erp_header_arithmetic(scenario):
    adapter, calls, _=endpoint(scenario)
    try: adapter.create_draft('cost-op', payload(scenario))
    finally: adapter.client.close()
    request, = [call for call in calls if call.method == 'POST']
    assert request.headers['Content-Type'] == 'application/json'
    decoded=json.loads(request.content, parse_float=Decimal)
    for key in ('discount_amount', 'additional_discount_percentage', 'conversion_rate'):
        assert isinstance(decoded[key], (int, Decimal)) and not isinstance(decoded[key], bool)
    for row in decoded['items']:
        for key in ('qty', 'rate', 'price_list_rate', 'discount_amount', 'discount_percentage'):
            assert isinstance(row[key], (int, Decimal))
    for row in decoded['taxes']:
        assert isinstance(row['rate'], (int, Decimal)) and isinstance(row['tax_amount'], (int, Decimal))
    # ERPNext v16.36.0 set_discount_amount tests this before multiplication.
    # The former string "0" enters this branch and raises TypeError.
    assert not decoded['additional_discount_percentage']
    assert Decimal(str(decoded['discount_amount'])) == Decimal(COST_CASES[scenario]['discount'])
    assert not isinstance(payload(scenario)['quote_values']['unit_price'], (int, Decimal))


def test_numeric_serializer_is_exact_and_does_not_mutate_snapshot_or_escape_text():
    from procureflow.erp import erp_numeric_json
    body={'discount_amount':'0.01','additional_discount_percentage':'0',
          'items':[{'rate':'999999.99','qty':'1','item_code':'Quoted " SKU\\end'}],
          'taxes':[{'rate':'12.3456','tax_amount':'123.45','description':'税费'}]}
    original=deepcopy(body)
    encoded=erp_numeric_json(body)
    decoded=json.loads(encoded, parse_float=Decimal)
    assert body==original and decoded['items'][0]['rate']==Decimal('999999.99')
    assert decoded['taxes'][0]['rate']==Decimal('12.3456')
    assert decoded['items'][0]['item_code']==body['items'][0]['item_code']
    assert b'"rate":999999.99' in encoded and b'"rate":12.3456' in encoded
    assert decoded['taxes'][0]['description']=='税费'


@pytest.mark.parametrize('value', ['NaN', 'Infinity', 'not-a-number', True, 0.1, None, {}])
def test_numeric_serializer_rejects_nonfinite_or_float_wire_values(value):
    from procureflow.erp import erp_numeric_json
    with pytest.raises(ERPRejected, match='NUMERIC_WIRE_INVALID'):
        erp_numeric_json({'discount_amount':value})


def test_erp_header_arithmetic_reproduces_previous_truthy_zero_typeerror():
    from procureflow.erp import erp_numeric_json
    def header_validation(values):
        # Source-equivalent control flow from tagged set_discount_amount:
        # Frappe has not generically coerced REST document header values yet.
        amount=values['discount_amount']
        if values['additional_discount_percentage']:
            amount=24800.0 * values['additional_discount_percentage'] / 100
        return amount <= 24800.0
    old={'additional_discount_percentage':'0','discount_amount':'0.00'}
    with pytest.raises(TypeError): header_validation(old)
    assert header_validation(json.loads(erp_numeric_json(old)))
    nonzero={'additional_discount_percentage':'0','discount_amount':'113.00'}
    assert header_validation(json.loads(erp_numeric_json(nonzero)))
