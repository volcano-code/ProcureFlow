"""No paid provider: actual SQLite, immutable library, advice and authenticated API.

ScriptAgent is a deterministic adapter fixture, not an LLM quality result.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/api'))
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio
import json
import httpx
import pytest
from fastapi import Depends
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient
from sqlalchemy import select, func, update
from procureflow.advice import AdviceService
from procureflow.advice_references import validate_binding, validate_result
from procureflow.config import Settings
from procureflow.contracts import Principal, RequestCreate, AdviceRunCommand
from procureflow.db import Database, AdviceRunRow, PolicyDocumentRevisionRow, EventRow, OperationRow, ApprovalRow, QuoteRow
from procureflow.domain import digest
from procureflow.erp import MockERP
from procureflow.errors import DomainError
from procureflow.service import ProcurementService
from procureflow.policy_documents import PolicyDocumentService, PublishDocument
from procureflow.policy_library_api import create_library_api
from procureflow.mcp_bridge import AdviceBridge, BridgeConfig, BridgeError
from procureflow.read_tools import validate_tool_arguments, invoke_snapshot

BUYER=Principal(user_id='buyer-01',tenant_id='demo',role='buyer')
APPROVER=Principal(user_id='approver-01',tenant_id='demo',role='approver')
OTHER=Principal(user_id='other-01',tenant_id='other',role='buyer')

@pytest.fixture
def setup(tmp_path):
    settings=Settings(data_dir=tmp_path)
    db=Database(settings.database_url,create_schema=True)
    procurement=ProcurementService(db,settings,MockERP(tmp_path/'erp.sqlite'))
    library=PolicyDocumentService(procurement)
    advice=AdviceService(procurement,library)
    request=procurement.create_request(BUYER,RequestCreate(title='Reference advice test',sku='STAND-01',quantity='20',budget='30000',max_delivery_days=14))
    yield procurement,library,advice,request
    db.engine.dispose()

def publish(library,version=0,**kw):
    body=dict(expected_version=version,title='运费核对',source_label='合成参考文本',
        content='运费未明确时应当向供应商澄清；不能默认为零。\n参考文档不授予审批权限。',reason='测试制度来源发布')
    body.update(kw)
    return library.publish(APPROVER,'freight',PublishDocument(**body))

def reserve(data,key='reference-advice-01'):
    procurement,_,advice,request=data
    current=procurement.get_request(BUYER,request['id'])
    return advice.reserve(BUYER,request['id'],AdviceRunCommand(expected_version=current['version'],idempotency_key=key))

def read(data,run,query='运费'):
    return data[2].reference_tool(BUYER,run['id'],'search_references',{'query':query})

def counts(data):
    with data[0].db.transaction() as s:
        return [s.scalar(select(func.count()).select_from(t)) for t in (AdviceRunRow,QuoteRow,ApprovalRow,OperationRow,EventRow)]


def test_bound_search_and_exact_fragment_are_read_only(setup):
    _,library,advice,_=setup
    published=publish(library);run=reserve(setup);before=counts(setup)
    binding=run['input_snapshot']['reference_library'];validate_binding(binding)
    result=read(setup,run)
    assert result['input_hash']==run['input_hash']
    hit=result['result']['results'][0]
    assert hit['revision_id']==published['id'] and hit['version']==1
    assert hit['fragment']['text']==library.revision(BUYER,published['id'])['content']
    exact=advice.reference_tool(BUYER,run['id'],'get_reference_fragment',{'citation_id':hit['citation_id']})
    assert exact['result']['results'][0]['fragment']==hit['fragment']
    assert counts(setup)==before

@pytest.mark.parametrize('change',['replace','withdraw','scope','expire','activate'])
def test_reference_changes_invalidate_run_not_procurement_rules(setup,change):
    procurement,library,advice,_=setup
    at=datetime.now(timezone.utc)
    if change=='expire':
        publish(library,expires_at=at+timedelta(seconds=60))
    else:publish(library)
    run=reserve(setup);rules=procurement.policy(BUYER)
    if change=='replace':publish(library,1,content='运费按新版核对，不自动批准。')
    elif change=='withdraw':publish(library,1,withdrawn=True,content='')
    elif change=='scope':publish(library,1,scope='finance')
    else:
        # Inject only the clock, not source rows, to exercise scheduled transitions.
        if change=='activate':publish(library,1,effective_at=at+timedelta(seconds=60),content='未来运费条款。')
        from unittest.mock import patch
        with patch('procureflow.policy_documents.datetime') as clock:
            clock.now.return_value=at+timedelta(seconds=120)
            assert advice.get(BUYER,run['id'])['current'] is False
            with pytest.raises(DomainError):read(setup,run)
        assert procurement.policy(BUYER)==rules
        return
    assert advice.get(BUYER,run['id'])['current'] is False
    with pytest.raises(DomainError):read(setup,run)
    assert procurement.policy(BUYER)==rules


def test_empty_corpus_is_bound_and_later_publication_invalidates(setup):
    run=reserve(setup)
    assert read(setup,run)['result']['status']=='no_match'
    publish(setup[1])
    assert setup[2].get(BUYER,run['id'])['current'] is False


def test_scheduled_revision_does_not_prematurely_stale_run(setup):
    publish(setup[1]);run=reserve(setup)
    publish(setup[1],1,effective_at=datetime.now(timezone.utc)+timedelta(days=1),content='未来运费文本')
    assert setup[2].get(BUYER,run['id'])['current'] is True
    assert read(setup,run)['result']['results'][0]['version']==1


def test_cross_tenant_and_cross_corpus_denied(setup):
    publish(setup[1]);run=reserve(setup)
    with pytest.raises(DomainError) as e:setup[2].reference_tool(OTHER,run['id'],'search_references',{'query':'运费'})
    assert e.value.status_code==404
    with pytest.raises(DomainError) as e:setup[2].reference_tool(BUYER,run['id'],'get_reference_fragment',{'citation_id':'pdr_'+'a'*32+':0'})
    assert e.value.code=='TOOL_SCOPE_DENIED'

@pytest.mark.parametrize('args',[{'query':'运费','scope':'finance'},{'query':'运费','method':'hybrid'},
    {'query':'运费','tenant_id':'other'}, {'query':'运费','url':'https://example.com'},
    {'query':'运费','limit':True},{'query':'运费','limit':6},{'query':'x'}, {'query':'运费','limit':'3'}])
def test_model_cannot_override_context(args):
    with pytest.raises(DomainError):validate_tool_arguments('search_references',args)


def test_snapshot_only_dispatch_cannot_fake_live_reference_search():
    with pytest.raises(DomainError) as e:invoke_snapshot({},'search_references',{'query':'运费'})
    assert e.value.code=='ADVICE_REFERENCE_UNBOUND'


class ScriptAgent:
    def __init__(self,effect=None,unknown=False):
        self.client=SimpleNamespace(close=lambda:None)
        self.effect,self.unknown=effect,unknown
    def run(self,invoke,evidence_ids,observer=None):
        comparison=invoke('get_comparison',{})
        assert comparison['request']
        result=invoke('search_references',{'query':'运费'})
        hit=result['results'][0]
        exact=invoke('get_reference_fragment',{'citation_id':hit['citation_id']})
        assert exact['results'][0]['fragment']==hit['fragment']
        if self.effect:self.effect()
        return {'summary':'运费未知需要澄清，参考文本不改变审批规则。',
            'evidence_ids':['forged-reference'] if self.unknown else [hit['citation_id']],
            'advisory_only':True,'llm_used':False,'semantic_factuality_verified':False}


def test_real_service_saves_server_bound_citations_without_procurement_writes(setup):
    publish(setup[1]);run=reserve(setup);before=counts(setup)
    result=setup[2].process(BUYER,run['id'],ScriptAgent)
    assert result['status']=='COMPLETED',result
    hit=result['output']['reference_citations'][0]
    assert result['output']['evidence_ids']==[hit['citation_id']]
    assert len(result['output']['reference_retrievals'])==2
    assert result['output']['reference_binding']==run['input_snapshot']['reference_library']
    after=counts(setup)
    assert before[:4]==after[:4] # advice audit events are permitted, business writes aren't


def test_publication_during_model_work_preserves_history_but_invalidates_result(setup):
    publish(setup[1]);run=reserve(setup)
    result=setup[2].process(BUYER,run['id'],lambda:ScriptAgent(lambda:publish(setup[1],1,withdrawn=True,content='')))
    assert result['status']=='STALE' and result['current'] is False
    assert result['output']['reference_citations'][0]['version']==1


def test_forged_output_citation_rejected_independently(setup):
    publish(setup[1]);run=reserve(setup)
    result=setup[2].process(BUYER,run['id'],lambda:ScriptAgent(unknown=True))
    assert result['status']=='FAILED' and result['error_code']=='EVIDENCE_NOT_FOUND'
    assert result['output'] is None


def test_legacy_run_does_not_attach_new_references_silently(setup):
    run=reserve(setup)
    with setup[0].db.transaction(write=True) as s:
        row=s.get(AdviceRunRow,run['id']);body=deepcopy(row.input_snapshot);body.pop('reference_library')
        row.input_snapshot=body;row.input_hash=digest(body)
    assert setup[2].get(BUYER,run['id'])['stale_reason']=='ADVICE_REFERENCE_UNBOUND'
    with pytest.raises(DomainError):read(setup,run)


def test_search_result_changed_between_read_and_disclosure_is_rejected(setup,monkeypatch):
    publish(setup[1]);run=reserve(setup);search=setup[1].search
    def racing(*args,**kwargs):
        result=search(*args,**kwargs);publish(setup[1],1,content='运费新要求');return result
    monkeypatch.setattr(setup[1],'search',racing)
    with pytest.raises(DomainError) as e:read(setup,run)
    assert e.value.code=='ADVICE_INPUT_CHANGED'


def test_corrupt_original_reference_is_not_used(setup):
    row=publish(setup[1]);run=reserve(setup)
    with setup[0].db.engine.begin() as c:c.execute(update(PolicyDocumentRevisionRow).where(
        PolicyDocumentRevisionRow.id==row['id']).values(content='corrupted'))
    assert setup[2].get(BUYER,run['id'])['stale_reason']=='POLICY_DOCUMENT_INTEGRITY'
    with pytest.raises(DomainError):read(setup,run)

@pytest.mark.parametrize('field,value',[('source_hash','0'*64),('version',99),('revision_id','pdr_'+'f'*32),
    ('trust','trusted'),('advisory_only',False),('citation_id','invented')])
def test_result_identity_tamper_rejected(setup,field,value):
    publish(setup[1]);run=reserve(setup);result=read(setup,run)['result']
    result['results'][0][field]=value
    with pytest.raises(DomainError):validate_result(run['input_snapshot']['reference_library'],result)


def test_authenticated_bound_http_reads(setup):
    procurement,library,advice,_=setup;publish(library);run=reserve(setup)
    bearer=HTTPBearer(auto_error=False)
    def identity(credentials=Depends(bearer)):
        if not credentials:raise DomainError('UNAUTHENTICATED','Authentication required',401)
        return procurement.identities.authenticate(credentials.credentials)
    with TestClient(create_library_api(procurement,identity,advice_service=advice)) as client:
        path=f'/advice-runs/{run["id"]}/search'
        assert client.get(path,params={'query':'运费'}).status_code==401
        headers={'Authorization':'Bearer demo-buyer'}
        result=client.get(path,headers=headers,params={'query':'运费'});assert result.status_code==200,result.text
        citation=result.json()['result']['results'][0]['citation_id']
        assert client.get(f'/advice-runs/{run["id"]}/fragment',headers=headers,params={'citation_id':citation}).status_code==200
        assert client.get(path,headers={'Authorization':'Bearer demo-other-tenant'},params={'query':'运费'}).status_code==404
        publish(library,1,withdrawn=True,content='')
        assert client.get(path,headers=headers,params={'query':'运费'}).status_code==409


def test_mcp_three_get_binding_and_revocation_guard(setup):
    publish(setup[1]);run=reserve(setup);envelope=read(setup,run);seen=[]
    def handler(req):
        seen.append(req)
        if req.url.path.endswith('/search'):return httpx.Response(200,json=envelope)
        return httpx.Response(200,json=run)
    async def check():
        bridge=AdviceBridge(BridgeConfig(base_url='http://127.0.0.1:8000',token='synthetic',advice_run_id=run['id']),transport=httpx.MockTransport(handler))
        try:
            result=await bridge.call('search_references',{'query':'运费'})
            assert result['result']['results'][0]['version']==1
        finally:await bridge.close()
    asyncio.run(check())
    assert len(seen)==3 and all(req.method=='GET' for req in seen)
    assert seen[1].url.params['query']=='运费'


def test_mcp_rejects_cross_receipt_response(setup):
    publish(setup[1]);run=reserve(setup);envelope=read(setup,run);envelope['input_hash']='0'*64
    def handler(req):return httpx.Response(200,json=envelope if req.url.path.endswith('/search') else run)
    async def check():
        bridge=AdviceBridge(BridgeConfig(base_url='http://127.0.0.1:8000',token='synthetic',advice_run_id=run['id']),transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(BridgeError,match='MCP_CONTEXT_INVALID'):await bridge.call('search_references',{'query':'运费'})
        finally:await bridge.close()
    asyncio.run(check())
