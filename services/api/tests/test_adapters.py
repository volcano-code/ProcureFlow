import json
import pytest
import httpx
from procureflow.agent import ReadOnlyAgent
from procureflow.errors import DomainError
from procureflow.erp import ERPNextClient, ERPRejected, ERPUnknown, remote_matches


def completion(message):
    return httpx.Response(200,json={"model":"test-model","choices":[{"message":message}],"usage":{"prompt_tokens":15,"completion_tokens":6}})


def test_real_http_adapter_tool_loop_mock_transport():
    calls=[]
    def handle(request):
        payload=json.loads(request.content)
        assert "a-secret-token" not in json.dumps(payload)
        calls.append(payload)
        if len(calls)==1:
            return completion({"role":"assistant","content":None,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_comparison","arguments":"{}"}}]})
        return completion({"role":"assistant","content":json.dumps({"summary":"报价运费未知，需要补充。","evidence_ids":["doc:f1"]})})
    agent=ReadOnlyAgent("https://model.test","a-secret-token","test-model",transport=httpx.MockTransport(handle))
    output=agent.run(lambda name,args:{"shipping_cost":None}, {"doc:f1"})
    assert output["llm_used"] is True and output["model_calls"]==2 and output["tool_calls"]==1
    assert output["advisory_only"] is True
    assert output["semantic_factuality_verified"] is False
    agent.client.close()


@pytest.mark.parametrize("name",["create_erp_draft","approve","execute_shell","search_web"])
def test_agent_write_and_undeclared_tools_denied(name):
    agent=ReadOnlyAgent("https://model.test","secret","test-model",transport=httpx.MockTransport(lambda _:completion({
        "role":"assistant","tool_calls":[{"id":"x","type":"function","function":{"name":name,"arguments":"{}"}}]})))
    invoked=[]
    with pytest.raises(DomainError) as exc:
        agent.run(lambda *x:invoked.append(x),set())
    assert exc.value.code=="TOOL_POLICY_DENIED" and invoked==[]
    agent.client.close()


def test_agent_fake_evidence_denied():
    agent=ReadOnlyAgent("https://model.test","secret","test-model",transport=httpx.MockTransport(lambda _:completion({
        "content":json.dumps({"summary":"x","evidence_ids":["nonexistent"]})})))
    with pytest.raises(DomainError) as exc:
        agent.run(lambda *x:None,set())
    assert exc.value.code=="EVIDENCE_NOT_FOUND"
    agent.client.close()


def test_agent_schema_validation():
    agent=ReadOnlyAgent("https://model.test","secret","test-model",transport=httpx.MockTransport(lambda _:completion({"content":"plain non-JSON"})))
    with pytest.raises(DomainError) as exc:
        agent.run(lambda *x:None,set())
    assert exc.value.code=="MODEL_SCHEMA_INVALID"
    agent.client.close()


def test_agent_bounded_loop():
    from itertools import count
    ids = count()
    agent=ReadOnlyAgent("https://model.test","secret","test-model",max_model_calls=2,transport=httpx.MockTransport(lambda _:completion({
        "tool_calls":[{"id":f"call-{next(ids)}","type":"function","function":{"name":"get_comparison","arguments":"{}"}}]})))
    with pytest.raises(DomainError) as exc:
        agent.run(lambda *x:{},set())
    assert exc.value.code=="BUDGET_EXCEEDED"
    agent.client.close()


def test_agent_extra_tool_arguments_rejected():
    agent=ReadOnlyAgent("https://model.test","secret","test-model",transport=httpx.MockTransport(lambda _:completion({
        "tool_calls":[{"id":"x","type":"function","function":{"name":"get_comparison","arguments":'{"role":"admin"}'}}]})))
    with pytest.raises(DomainError) as exc:
        agent.run(lambda *x:{},set())
    assert exc.value.code=="TOOL_ARGUMENTS_INVALID"
    agent.client.close()


def payload():
    return {"snapshot_hash":"a"*64,"erp_company":"Demo","transaction_date":"2026-09-22","total":"200.00", "quote_values":{
        "supplier_id":"SUP-A","sku":"STAND-01","quantity":"2","uom":"EA","unit_price":"100.00",
        "tax_mode":"excluded","tax_rate":"0","shipping_cost":"0","discount":"0","currency":"CNY"}}


def test_erp_rest_contract_draft_only():
    seen=[]
    persisted={}
    def handle(request):
        seen.append(request)
        assert request.headers['Authorization']=='token key:secret'
        if request.url.path.endswith('/Custom Field'):
            field=json.loads(request.url.params['filters'])[-1][-1]
            return httpx.Response(200,json={"data":[{"fieldname":field,"unique":1,"fieldtype":"Data"}]})
        if request.method=='POST':
            body=json.loads(request.content)
            assert body['docstatus']==0 and body['doctype']=='Supplier Quotation'
            assert body['custom_procureflow_operation_key']=='op-123'
            body.update(name='SQ-TEST',grand_total='200.00')
            persisted.update(body)
            return httpx.Response(200,json={'data':body})
        if request.url.path.endswith('/SQ-TEST'):
            return httpx.Response(200,json={'data':persisted})
        return httpx.Response(200,json={'data':[]})
    adapter=ERPNextClient('https://erp.test','key','secret','Demo',True,transport=httpx.MockTransport(handle))
    result=adapter.create_draft('op-123',payload())
    assert result['name']=='SQ-TEST' and remote_matches(result,payload())
    assert len([x for x in seen if x.method=='POST'])==1
    assert all('submit' not in str(x.url) for x in seen)
    adapter.client.close()


def test_erp_disabled_before_network():
    def unexpected(_):raise AssertionError('No network call was allowed')
    adapter=ERPNextClient('https://erp.test','key','secret','Demo',False,transport=httpx.MockTransport(unexpected))
    with pytest.raises(ERPRejected,match='ERP_DRAFT_WRITES_DISABLED'):
        adapter.create_draft('op',payload())
    adapter.client.close()


def test_erp_complex_mapping_fails_closed():
    adapter=ERPNextClient('https://erp.test','key','secret','Demo',True,transport=httpx.MockTransport(lambda _:None))
    p=payload();p['quote_values']['shipping_cost']='800'
    with pytest.raises(ERPRejected,match='MAPPING_NOT_IMPLEMENTED'):
        adapter.create_draft('op',p)
    adapter.client.close()


def test_erp_remote_uniqueness_required():
    adapter=ERPNextClient('https://erp.test','key','secret','Demo',True,transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'data':[]})))
    with pytest.raises(ERPRejected,match='ERP_UNIQUE_FIELD_NOT_VERIFIED'):
        adapter.create_draft('op',payload())
    adapter.client.close()


def test_erp_transport_ambiguity():
    def timeout(request):raise httpx.ReadTimeout('timeout',request=request)
    adapter=ERPNextClient('https://erp.test','key','secret','Demo',True,transport=httpx.MockTransport(timeout))
    with pytest.raises(ERPUnknown):adapter.find('op')
    adapter.client.close()


@pytest.mark.parametrize("field,value", [("company","AnotherCompany"),("uom","BOX")])
def test_remote_document_company_and_uom_must_match(field,value):
    p=payload()
    remote={"name":"SQ-TEST","docstatus":0,"snapshot_hash":p["snapshot_hash"],"supplier_id":"SUP-A",
            "sku":"STAND-01","currency":"CNY","quantity":"2","total":"200.00","company":"Demo","uom":"EA"}
    assert remote_matches(remote,p)
    remote[field]=value
    assert not remote_matches(remote,p)


def test_erp_company_bound_to_approval_before_network():
    adapter=ERPNextClient('https://erp.test','key','secret','Other',True,transport=httpx.MockTransport(lambda _:None))
    with pytest.raises(ERPRejected,match='ERP_COMPANY_SNAPSHOT_MISMATCH'):
        adapter.create_draft('op',payload())
    adapter.client.close()
