import json
import httpx
import pytest
from procureflow.erp import ERPNextClient, ERPUnknown, ERPRejected, remote_matches
from test_adapters import payload


def endpoint(*, company='Demo', snapshot_field=True, change_readback=None):
    calls=[];stored={}
    def handle(request):
        calls.append(request)
        path=request.url.path
        if path.endswith('/Custom Field'):
            field=json.loads(request.url.params['filters'])[-1][-1]
            fields=[{'fieldname':field,'fieldtype':'Data','unique':1}]
            if field==ERPNextClient.hash_field and not snapshot_field: fields=[]
            return httpx.Response(200,json={'data':fields})
        if '/Company/' in path: return httpx.Response(200,json={'data':{'name':company,'default_currency':'CNY'}})
        if path.endswith('/Supplier'):return httpx.Response(200,json={'data':[{'name':'SUP-A','supplier_name':'Synthetic'}]})
        if request.method=='POST':
            record=json.loads(request.content);record.update(name='SQ-READBACK',grand_total='200.00', total='200.00', net_total='200.00', total_taxes_and_charges='0');record['items'][0].update(amount='200.00', net_amount='200.00', net_rate='100.00');stored.update(record)
            return httpx.Response(200,json={'data':record})
        if path.endswith('/SQ-READBACK'):
            record=dict(stored)
            if change_readback:change_readback(record)
            return httpx.Response(200,json={'data':record})
        return httpx.Response(200,json={'data':[]})
    return calls,httpx.MockTransport(handle)


def test_readonly_preflight_performs_no_write_and_no_credential_in_report():
    seen,transport=endpoint()
    client=ERPNextClient('https://erp.test','test-key','test-secret','Demo',transport=transport)
    try: result=client.preflight()
    finally:client.client.close()
    assert result['status']=='read_only_checks_passed'
    assert result['write_probe_performed'] is False and result['live_draft_roundtrip_verified'] is False
    assert len(seen)==4 and all(r.method=='GET' for r in seen)
    assert 'test-secret' not in json.dumps(result) and 'SUP-A' not in json.dumps(result)


def test_missing_snapshot_field_denies_before_post():
    seen,transport=endpoint(snapshot_field=False)
    client=ERPNextClient('https://erp.test','k','s','Demo',True,transport=transport)
    try:
        with pytest.raises(ERPRejected,match='SNAPSHOT_FIELD_NOT_VERIFIED'):client.create_draft('op',payload())
        assert all(r.method=='GET' for r in seen)
    finally:client.client.close()


def test_company_mismatch_preflight_fails():
    seen,transport=endpoint(company='Other')
    client=ERPNextClient('https://erp.test','k','s','Demo',transport=transport)
    try:
        with pytest.raises(ERPRejected,match='COMPANY_NOT_VERIFIED'):client.preflight()
    finally:client.client.close()


@pytest.mark.parametrize('field,value',[
    ('custom_procureflow_operation_key','another-key'),('custom_procureflow_snapshot_hash','b'*64),
    ('grand_total','201.00'),('docstatus',1),('transaction_date','2026-09-23')])
def test_create_verifies_persisted_readback_not_post_echo(field,value):
    seen,transport=endpoint(change_readback=lambda record:record.update({field:value}))
    client=ERPNextClient('https://erp.test','k','s','Demo',True,transport=transport)
    try:
        with pytest.raises(ERPRejected):client.create_draft('op',payload())
        assert sum(r.method=='POST' for r in seen)==1
        assert seen[-1].method=='GET' and seen[-1].url.path.endswith('/SQ-READBACK')
    finally:client.client.close()


@pytest.mark.parametrize('data',[None,[],{'name':'bad','items':None}, {'name':'bad','items':[None]}])
def test_malformed_readback_is_uncertain_not_ordinary_python_exception(data):
    client=ERPNextClient('https://erp.test','k','s','Demo',transport=httpx.MockTransport(lambda _:None))
    try:
        with pytest.raises(ERPUnknown):client._normalize(data,expected_key='op')
    finally:client.client.close()


def test_filter_results_do_not_authorize_wrong_operation_key():
    def handle(request):
        if '/Company/' in request.url.path: return httpx.Response(200,json={'data':{'name':'Demo','default_currency':'CNY'}})
        if request.url.path.endswith('/Supplier Quotation'):return httpx.Response(200,json={'data':[{'name':'SQ'}]})
        p=payload();return httpx.Response(200,json={'data':{'name':'SQ','docstatus':0,'items':[{'qty':'2','rate':'100.00'}],
            'grand_total':'200.00','custom_procureflow_operation_key':'wrong','custom_procureflow_snapshot_hash':p['snapshot_hash']}})
    client=ERPNextClient('https://erp.test','k','s','Demo',transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ERPRejected,match='OPERATION_KEY_MISMATCH'):client.find('expected')
    finally:client.client.close()
