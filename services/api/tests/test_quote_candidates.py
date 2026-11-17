"""Candidate workflow security and provenance; all provider outputs here are fixtures."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, func
from conftest import BUYER, APPROVER, AUDITOR, OTHER, request, publish_policy
from procureflow.candidate_agent import CandidateAgent
from procureflow.candidate_contracts import CandidateProposal
from procureflow.db import QuoteCandidateRunRow, QuoteRow, DocumentRow, OperationRow
from procureflow.errors import DomainError

TEXT = ('Acme Ltd offers twenty STAND-01 units, EA, in CNY.\n'
        'Unit price 100.00, excluded tax at 0%, delivery in 7 days.\n'
        'Freight 0.00 and discount 0.00 are explicit.\n')
VALUES = dict(supplier_id='Acme Ltd', sku='STAND-01', quantity='20', uom='EA', currency='CNY',
              unit_price='100.00', tax_mode='excluded', tax_rate='0', delivery_days=7,
              shipping_cost='0.00', discount='0.00')


def source(client, rid, text=TEXT, filename='offer.txt', headers=BUYER, expected=201):
    response = client.post(f'/api/v1/requests/{rid}/candidate-sources', headers=headers,
                           files={'file':(filename,text.encode())})
    assert response.status_code == expected, response.text
    return response.json()


def reserve(client, rid, doc, key='candidate-0001', headers=BUYER, expected=201):
    version=client.get(f'/api/v1/requests/{rid}',headers=BUYER).json()['version']
    response=client.post(f'/api/v1/requests/{rid}/candidate-runs', headers=headers,
        json=dict(document_id=doc['id'], expected_version=version, idempotency_key=key))
    assert response.status_code == expected, response.text
    return response.json()


def process(client, run, headers=BUYER, expected=200):
    response=client.post(f"/api/v1/candidate-runs/{run['id']}/process",headers=headers,
        json={'expected_revision':run['revision'],'acknowledge_external_model':True})
    assert response.status_code == expected, response.text
    return response.json()


def import_run(client, run, headers=BUYER, expected=200):
    response=client.post(f"/api/v1/candidate-runs/{run['id']}/import", headers=headers,
        json={'expected_revision':run['revision'], 'acknowledge_unverified_candidates':True,
              'reason':'I reviewed the suggested fields against the source; still unconfirmed.'})
    assert response.status_code==expected,response.text
    return response.json()


class FakeCandidate:
    """A scripted runtime fixture, NOT a successful real LLM extraction experiment."""
    def __init__(self):
        self.calls=0;self.effect=None;self.change=None
        self.client=SimpleNamespace(close=lambda:None)
    def run(self, invoke, evidence_ids, observer=None):
        self.calls+=1
        invoke('get_request',{})
        evidence=invoke('get_evidence',{'start':0,'limit':24})
        values=copy.deepcopy(VALUES)
        if self.change:self.change(values)
        result={'summary':'字段候选，须人工核对来源。','values':values,
                'references':{k:[evidence['fragments'][0]['id']] for k,v in values.items()
                    if v is not None and not(k=='tax_mode' and v=='unknown')},
                'clarifications':[], 'context_evidence_ids':[evidence['fragments'][0]['id']],
                'runtime':'scripted-test-runtime', 'model_calls':1, 'tool_calls':2, 'usage':None,
                'usage_complete':False,'cost':None}
        if self.effect:self.effect(result)
        return result


@pytest.fixture
def candidate(monkeypatch):
    agent=FakeCandidate()
    monkeypatch.setattr(CandidateAgent,'from_env',staticmethod(lambda:agent))
    return agent


def make(system):
    client,service,erp=system
    req=request(client);doc=source(client,req['id']);run=reserve(client,req['id'],doc)
    return req,doc,run


def test_candidate_is_source_only_then_unconfirmed_then_normal_approval(system,candidate):
    client,service,erp=system
    req,doc,run=make(system)
    assert not client.get(f"/api/v1/requests/{req['id']}/quotes",headers=BUYER).json()
    done=process(client,run)
    assert done['status']=='NEEDS_REVIEW' and done['can_import']
    assert done['output']['values']['supplier_id']=='Acme Ltd'
    assert done['output']['calculation']['total']=='2000.00'
    assert 'FIELDS_NOT_CONFIRMED' in done['output']['calculation']['violations']
    assert done['semantic_factuality_verified'] is False
    quote=import_run(client,done)
    assert quote['confirmed_by'] is None and quote['evidence']['supplier_id']['origin']=='model_candidate'
    assert quote['evidence']['supplier_id']['document_sha256']==doc['sha256']
    # Replaying import returns the same quote, no new version or duplicate.
    assert import_run(client,done)['id']==quote['id']
    assert not client.post(f"/api/v1/requests/{req['id']}/analyze",headers=BUYER,json={}).json()['proposal']
    assert client.post(f"/api/v1/quotes/{quote['id']}/confirm",headers=BUYER,
        json={'expected_version':1,'acknowledge':True}).status_code==200
    proposal=client.post(f"/api/v1/requests/{req['id']}/analyze",headers=BUYER,json={}).json()['proposal']
    assert proposal['total']=='2000.00'
    # Model extraction and manual import did not grant execution authority.
    assert client.post(f"/api/v1/requests/{req['id']}/execute",headers=BUYER,
        json={'snapshot_hash':proposal['snapshot_hash']}).status_code==409
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OperationRow))==0
    assert candidate.calls==1


def test_missing_freight_remains_unknown_and_can_be_corrected_only_after_import(system,candidate):
    client,_,_=system;req,doc,run=make(system)
    candidate.change=lambda values:values.update(shipping_cost=None)
    done=process(client,run)
    assert done['status']=='NEEDS_CLARIFICATION' and done['output']['calculation']['total'] is None
    assert any(q['field']=='shipping_cost' for q in done['output']['clarifications'])
    quote=import_run(client,done)
    assert quote['values']['shipping_cost'] is None and quote['confirmed_by'] is None
    assert 'shipping_cost' not in quote['evidence']


@pytest.mark.parametrize('headers,expected',[(OTHER,404),(APPROVER,403),(AUDITOR,403),({},401)])
def test_candidate_operations_cannot_cross_scope_or_role(system,candidate,headers,expected):
    client,_,_=system;req,doc,run=make(system)
    process(client,run,headers,expected)
    assert candidate.calls==0
    if headers==OTHER:
        assert client.get(f"/api/v1/candidate-runs/{run['id']}",headers=headers).status_code==404
        reserve(client,req['id'],doc,headers=headers,expected=404)
    import_run(client,run,headers,expected)


def test_actor_scope_and_explicit_acknowledgements(system,candidate):
    client,_,_=system;_,_,run=make(system)
    for path,body in [('process',{'expected_revision':1}),('import',{'expected_revision':1,'reason':'reviewed field evidence'})]:
        assert client.post(f"/api/v1/candidate-runs/{run['id']}/{path}",headers=BUYER,json=body).status_code==422
    assert candidate.calls==0


def test_idempotency_key_is_bound_to_source(system):
    client,_,_=system;req,doc,run=make(system)
    assert reserve(client,req['id'],doc)['id']==run['id']
    other=source(client,req['id'],TEXT+'Changed source text.')
    reserve(client,req['id'],other,expected=409)
    assert source(client,req['id'])['id']==doc['id']


def test_unconfigured_provider_does_not_call_network(system,monkeypatch):
    monkeypatch.delenv('PF_ALLOW_CANDIDATE_MODEL',raising=False)
    client,_,_=system;_,_,run=make(system)
    done=process(client,run)
    assert done['status']=='FAILED' and done['error_code']=='CANDIDATE_MODEL_DISABLED'


def test_cancelled_pending_and_expired_running_are_not_replayed(system,candidate):
    client,service,_=system;_,_,run=make(system)
    cancelled=client.post(f"/api/v1/candidate-runs/{run['id']}/cancel",headers=BUYER,json={'expected_revision':1}).json()
    assert process(client,cancelled)['status']=='CANCELLED'
    with service.db.transaction(write=True) as session:
        row=session.get(QuoteCandidateRunRow,run['id']);row.status='RUNNING';row.lease_until='2000-01-01T00:00:00+00:00'
    assert process(client,cancelled)['status']=='INTERRUPTED'
    assert candidate.calls==0


@pytest.mark.parametrize('drift',['request','policy','source','snapshot','expiry'])
def test_bound_inputs_fail_closed_before_provider(system,candidate,drift):
    client,service,_=system;req,doc,run=make(system)
    if drift=='request':
        body={k:req[k] for k in ('title','sku','quantity','uom','currency','budget','max_delivery_days')}
        body.update(title='Changed',expected_version=req['version'])
        assert client.put(f"/api/v1/requests/{req['id']}",headers=BUYER,json=body).status_code==200
    elif drift=='policy':publish_policy(client,budget_cap='25000.00')
    else:
        with service.db.transaction(write=True) as session:
            row=session.get(QuoteCandidateRunRow,run['id'])
            if drift=='snapshot':row.input_hash='0'*64
            elif drift=='expiry':row.expires_at='2000-01-01T00:00:00+00:00'
            else:
                d=session.get(DocumentRow,doc['id']);(service.document_dir/d.storage_key).write_text('tampered')
    assert process(client,run)['status']=='STALE' and candidate.calls==0


def test_pending_runs_and_provenance_survive_new_app_instance(system,candidate):
    from fastapi.testclient import TestClient
    from procureflow.app import create_app
    client,service,erp=system;_,_,run=make(system)
    with TestClient(create_app(service.settings,service.db,erp)) as restarted:
        receipt=restarted.get(f"/api/v1/candidate-runs/{run['id']}",headers=BUYER).json()
        assert receipt['status']=='PENDING' and candidate.calls==0
        assert process(restarted,receipt)['status']=='NEEDS_REVIEW'


@pytest.mark.parametrize('mode',['unknown_ref','missing_field_ref','conflicting_known','extra_write','malformed','unverified_value'])
def test_untrusted_output_validation_and_semantic_boundary(system,candidate,mode):
    client,_,_=system;_,_,run=make(system)
    def change(result):
        if mode=='unknown_ref':result['references']['unit_price']=['other-doc:f0000']
        if mode=='missing_field_ref':result['references'].pop('unit_price')
        if mode=='conflicting_known':result['clarifications']=[{'field':'unit_price','question':'Which conflicting price is right?','reason':'conflict'}]
        if mode=='extra_write':result['values']['approved']=True
        if mode=='malformed':result['values']['quantity']='NaN'
        if mode=='unverified_value':result['values']['unit_price']='9.99'
    candidate.effect=change
    done=process(client,run)
    if mode=='unverified_value':
        # Location verification alone cannot decide semantic truth. Keep it advisory.
        assert done['status']=='NEEDS_REVIEW' and not done['semantic_factuality_verified']
        assert import_run(client,done)['confirmed_by'] is None
    else:
        assert done['status']=='FAILED' and done['output'] is None


def test_context_only_citations_support_asking_without_invented_fields(system,candidate):
    client,_,_=system;_,_,run=make(system)
    candidate.change=lambda values:values.update({k:'unknown' if k=='tax_mode' else None for k in values})
    done=process(client,run)
    assert done['status']=='NEEDS_CLARIFICATION' and done['output']['references']=={}
    assert done['output']['context_evidence_ids']
    assert done['output']['calculation']['total'] is None


def test_cancel_during_provider_discards_result_and_second_process_does_not_call(system,candidate):
    client,_,_=system;_,_,run=make(system)
    entered,release=Event(),Event()
    def effect(result):entered.set();assert release.wait(10)
    candidate.effect=effect
    with ThreadPoolExecutor(2) as pool:
        f=pool.submit(process,client,run)
        assert entered.wait(10)
        current=client.get(f"/api/v1/candidate-runs/{run['id']}",headers=BUYER).json()
        # Duplicate process returns current receipt without contacting provider again.
        assert process(client,current)['status']=='RUNNING'
        assert client.post(f"/api/v1/candidate-runs/{run['id']}/cancel",headers=BUYER,
            json={'expected_revision':current['revision']}).json()['status']=='CANCELLED'
        release.set();done=f.result()
    assert done['status']=='CANCELLED' and done['output'] is None and candidate.calls==1


def test_candidate_output_integrity_fences_manual_import(system,candidate):
    client,service,_=system;_,_,run=make(system);done=process(client,run)
    with service.db.transaction(write=True) as session:
        row=session.get(QuoteCandidateRunRow,run['id']);payload=copy.deepcopy(row.output)
        payload['values']['unit_price']='1.00';row.output=payload
    import_run(client,done,expected=409)
    result=client.get(f"/api/v1/candidate-runs/{run['id']}",headers=BUYER).json()
    assert not result['can_import'] and result['output'] is None


@pytest.mark.parametrize('text,filename,code',[('','offer.txt',413),('x'*16001,'offer.txt',413),('x','offer.exe',415),('\n'.join('x' for _ in range(129)),'offer.txt',413)], ids=['empty','text-limit','extension','fragment-limit'])
def test_candidate_source_limits_and_no_unsupported_format_fallback(system,text,filename,code):
    client,_,_=system;req=request(client)
    source(client,req['id'],text,filename,expected=code)


def test_real_graph_runtime_uses_scoped_reads_and_no_write_tools():
    seen=[]
    def transport(req):
        body=json.loads(req.content);seen.append(body)
        assert {t['function']['name'] for t in body['tools']}=={'get_request','get_source_index','get_evidence'}
        if len(seen)==1:
            message={'role':'assistant','content':None,'tool_calls':[
                {'id':'request-1','type':'function','function':{'name':'get_request','arguments':'{}'}},
                {'id':'evidence-1','type':'function','function':{'name':'get_evidence','arguments':'{"limit":1}'}}]}
            finish='tool_calls'
        else:
            proposal=dict(summary='待核对报价',values=VALUES,references={k:['doc:f0000'] for k in VALUES},clarifications=[])
            message={'role':'assistant','content':json.dumps(proposal)};finish='stop'
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':finish}],
            'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}})
    agent=CandidateAgent('https://model.example.invalid','fake-key','fixture-model',transport=httpx.MockTransport(transport))
    try:
        result=agent.run(lambda name,args:{'fragments':[{'id':'doc:f0000','text':TEXT}]} if name=='get_evidence' else {},{'doc:f0000'})
    finally:agent.client.close()
    assert result['values']['unit_price']=='100.00' and result['model_calls']==2 and result['tool_calls']==2
    assert result['runtime']=='langgraph-quote-candidate-v1' and result['evidence_read_verified']
    assert result['cost'] is None and not result['semantic_factuality_verified']


@pytest.mark.parametrize('content',['{','null','{"summary":"one","summary":"two"}'])
def test_malformed_model_candidate_has_safe_schema_error(content):
    agent=CandidateAgent('https://model.example.invalid','fake','fixture')
    try:
        with pytest.raises(DomainError) as error:agent.decode_output(content)
        assert error.value.code=='MODEL_SCHEMA_INVALID'
    finally:agent.client.close()


@pytest.mark.parametrize('attack,expected',[
    ('execute','TOOL_POLICY_DENIED'),('scope','TOOL_ARGUMENTS_INVALID'),
    ('unread','EVIDENCE_NOT_READ'),('unknown','EVIDENCE_NOT_FOUND'),
    ('no_tools','MODEL_GROUNDING_REQUIRED'),('loop','BUDGET_EXCEEDED')])
def test_candidate_actual_graph_rejects_injected_actions_and_ungrounded_output(attack,expected):
    count=0
    def handle(req):
        nonlocal count
        count+=1
        if count==1 or attack=='loop':
            name='execute' if attack=='execute' else 'get_evidence'
            args={'document_id':'other-tenant-document'} if attack=='scope' else {'limit':1}
            calls=[{'id':f'evidence-{count}','type':'function','function':{'name':name,'arguments':json.dumps(args)}},
                   {'id':f'request-{count}','type':'function','function':{'name':'get_request','arguments':'{}'}}]
            message={'tool_calls':calls}
        else:
            ref='doc:f0001' if attack=='unread' else 'other-doc' if attack=='unknown' else 'doc:f0000'
            message={'content':json.dumps(dict(summary='候选须核对',values=VALUES,references={k:[ref] for k in VALUES}))}
        if attack=='no_tools':message={'content':json.dumps(dict(summary='没有读取证据',values=VALUES,references={k:['doc:f0000'] for k in VALUES}))}
        return httpx.Response(200,json={'choices':[{'message':message}]})
    agent=CandidateAgent('https://model.test','test-key','fixture',transport=httpx.MockTransport(handle),max_model_calls=2)
    try:
        with pytest.raises(DomainError) as error:
            agent.run(lambda name,args:{'fragments':[{'id':'doc:f0000','text':TEXT}]} if name=='get_evidence' else {},{'doc:f0000','doc:f0001'})
        assert error.value.code==expected
    finally:agent.client.close()


def test_policy_change_while_provider_runs_discards_result(system,candidate):
    client,_,_=system;_,_,run=make(system)
    candidate.effect=lambda result:publish_policy(client,budget_cap='25000.00')
    done=process(client,run)
    assert done['status']=='STALE' and done['output'] is None
    import_run(client,done,expected=409)


def test_manual_import_version_race_is_rejected(system,candidate):
    client,_,_=system;_,_,run=make(system);done=process(client,run)
    stale={**done,'revision':1}
    import_run(client,stale,expected=409)
    assert import_run(client,done)['confirmed_by'] is None


def test_scoped_document_cannot_be_rebound_to_another_request(system):
    client,_,_=system;_,doc,_=make(system);other=request(client)
    reserve(client,other['id'],doc,key='other-request-key',expected=404)


@pytest.mark.parametrize('ack', [1, 'true', False, None])
def test_only_explicit_boolean_ack_can_authorize_process_or_import(system,candidate,ack):
    client,_,_=system;_,_,run=make(system)
    for action,field in [('process','acknowledge_external_model'),('import','acknowledge_unverified_candidates')]:
        body={'expected_revision':1,field:ack}
        if action=='import':body['reason']='reviewed source evidence'
        r=client.post(f"/api/v1/candidate-runs/{run['id']}/{action}",headers=BUYER,json=body)
        assert r.status_code==422
    assert candidate.calls==0


def test_revoked_pilot_identity_during_call_discards_candidate(tmp_path):
    from procureflow.config import Settings
    from procureflow.db import Database
    from procureflow.erp import MockERP
    from procureflow.service import ProcurementService
    from procureflow.candidates import CandidateService
    from procureflow.contracts import RequestCreate
    from procureflow.candidate_contracts import CandidateReserve, CandidateProcess
    db=Database(f"sqlite:///{tmp_path}/pilot-candidate.sqlite3",create_schema=True)
    settings=Settings(data_dir=tmp_path, database_url=db.engine.url.render_as_string(hide_password=False),mode='pilot')
    service=ProcurementService(db,settings,MockERP(tmp_path/'mock-erp.sqlite3'))
    identity=service.identities;identity.create_tenant('candidate-pilot')
    identity.set_membership('candidate-pilot','buyer','buyer')
    login=identity.login(identity.issue_invite('candidate-pilot','buyer')['credential'])
    principal=identity.authenticate(login['token'])
    req=service.create_request(principal,RequestCreate(title='Pilot candidate fixture',sku='STAND-01',quantity='20',budget='3000',max_delivery_days=14))
    candidates=CandidateService(service)
    doc=candidates.upload(principal,req['id'],'offer.txt',TEXT.encode())
    run=candidates.reserve(principal,req['id'],CandidateReserve(document_id=doc['id'],expected_version=req['version'],idempotency_key='pilot-candidate-1'))
    agent=FakeCandidate();agent.effect=lambda result:identity.set_membership('candidate-pilot','buyer','buyer',active=False)
    with pytest.raises(DomainError):
        candidates.process(principal,run['id'],CandidateProcess(expected_revision=1,acknowledge_external_model=True),lambda:agent)
    with db.transaction() as session:
        record=session.get(QuoteCandidateRunRow,run['id'])
        assert record.status=='FAILED' and record.error_code=='MODEL_CANCELLED' and record.output is None
    db.engine.dispose()
