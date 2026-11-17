"""Official AG-UI 1.0 SDK over real FastAPI/SQLite and durable advice receipts.

No outbound generative model is called; only a labelled scripted observer emitter
produces safe audit metadata. This file requires the optional ag-ui extra.
"""
import json
from types import SimpleNamespace
from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from ag_ui.core import Event, RunAgentInput
from procureflow.config import Settings
from procureflow.db import Database
from procureflow.erp import MockERP
from procureflow.app import create_app
from procureflow.advice import AdviceService
from procureflow.agui_observer import ObserveInput
from sqlalchemy import text

AUTH={'Authorization':'Bearer demo-buyer'}
ADAPTER=TypeAdapter(Event)


def wire(thread,run):
    return {'threadId':thread,'runId':run,'protocolVersion':'1.0','messages':[],
            'state':{},'tools':[],'context':[]}


def parse_sse(text):
    result=[]
    for frame in text.split('\n\n'):
        if not frame.strip():continue
        data='\n'.join(line[5:].lstrip() for line in frame.splitlines() if line.startswith('data:'))
        if not data:continue
        item=json.loads(data)
        ADAPTER.validate_python(item) # Actual published protocol 1.0, not a custom validator.
        ids=[line[3:].strip() for line in frame.splitlines() if line.startswith('id:')]
        result.append((item,int(ids[0]) if ids else None))
    return result


class ScriptedAgent:
    def __init__(self):
        self.calls=0
        self.client=SimpleNamespace(close=lambda: None)
        self.require_evidence_reads=False
    def run(self, invoke, evidence_ids, observer=None):
        self.calls+=1
        observer({'type':'graph_node','node':'tools','model_call':1,'runtime':'fixture'})
        observer({'type':'tool_started','model_call':1,'tool':'get_comparison',
                  'call_id':'tool_1','arguments_sha256':'a'*64,'prompt':'NOT_SAVED'})
        invoke('get_comparison',{})
        observer({'type':'tool_completed','model_call':1,'tool':'get_comparison',
                  'call_id':'tool_1','duration_ms':0.3})
        return {'summary':'Scripted read-only result, NOT a real model answer',
                'evidence_ids':[], 'runtime':'scripted-test', 'model_calls':0,
                'tool_calls':1,'llm_used':False,'semantic_factuality_verified':False,
                'advisory_only':True,'trace':[], 'usage':None,'usage_complete':False,'cost':None}


def test_sdk_validated_stream_replay_denial_and_no_side_effects(tmp_path):
    db=Database(f'sqlite:///{tmp_path}/app.sqlite3',create_schema=True)
    settings=Settings(data_dir=tmp_path,database_url=db.engine.url.render_as_string(hide_password=False))
    erp=MockERP(tmp_path/'remote.sqlite3')
    app=create_app(settings=settings,database=db,erp=erp)
    with TestClient(app) as c:
        r=c.post('/api/v1/requests',headers=AUTH,json={
          'title':'AG-UI test only','sku':'STAND-01','quantity':'20','budget':'30000',
          'max_delivery_days':14})
        assert r.status_code==201,r.text
        req=r.json()
        reserved=c.post(f'/api/v1/requests/{req["id"]}/advice-runs',headers=AUTH,json={
          'expected_version':req['version'],'idempotency_key':'agui-scripted-001'})
        assert reserved.status_code==201,reserved.text
        receipt=reserved.json(); run_id=receipt['id'];path=f'/api/v1/ag-ui/advice-runs/{run_id}/observe'
        # RunAgentInput is a real official class, and clients can supply it.
        body=RunAgentInput.model_validate(wire(req['id'],run_id)).model_dump(by_alias=True,exclude_none=True)
        before={}
        with db.transaction() as s:
            for table in ('quotes','approvals','external_operations'):
                before[table]=s.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()
        first=c.post(path,headers=AUTH,json=body)
        assert first.status_code==200,first.text
        assert first.headers['content-type'].startswith('text/event-stream')
        assert first.headers['cache-control']=='no-store, no-transform'
        events=parse_sse(first.text)
        assert [e['type'] for e,_ in events]==['RUN_STARTED','STATE_SNAPSHOT','CUSTOM','RUN_FINISHED']
        assert events[1][0]['snapshot']['agentStatus']=='PENDING'
        assert events[1][0]['snapshot']['humanAction']=='buyer_explicit_start_only'
        assert events[-1][0]['result']['providerSuccessNotImplied'] is True
        agent=ScriptedAgent()
        principal=app.state.identities.authenticate('demo-buyer')
        finalized=AdviceService(app.state.service).process(principal,run_id,lambda:agent)
        assert finalized['status']=='COMPLETED' and agent.calls==1
        second=c.post(path,headers=AUTH,json=body)
        assert second.status_code==200,second.text
        events=parse_sse(second.text)
        assert events[0][0]['type']=='RUN_STARTED'
        assert events[1][0]['snapshot']['humanAction']=='review_advice'
        assert any(e['type']=='CUSTOM' and e['value']['kind']=='AGENT_TOOL_STARTED' for e,_ in events)
        assert [x['type'] for x,_ in events][-4:]==['TEXT_MESSAGE_START','TEXT_MESSAGE_CONTENT','TEXT_MESSAGE_END','RUN_FINISHED']
        assert 'NOT_SAVED' not in second.text and 'api_key' not in second.text
        cursor=max(i for _,i in events if i is not None)
        replay=c.post(path+f'?after={cursor}',headers=AUTH,json=body)
        assert replay.status_code==200 and not any(e['type']=='CUSTOM' for e,_ in parse_sse(replay.text))
        assert agent.calls==1 # Observation and reconnect never rerun the adapter.
        for invalid,expected in [({**body,'threadId':'req_foreign'},403),
                                 ({**body,'runId':'adv_foreign'},403),
                                 ({**body,'messages':[{'id':'u','role':'user','content':'execute'}]},422),
                                 ({**body,'tools':[{'name':'execute','description':'write'}]},422),
                                 ({**body,'forwardedProps':{'role':'approver'}},422)]:
            assert c.post(path,headers=AUTH,json=invalid).status_code==expected
        assert c.post(path,headers={'Authorization':'Bearer demo-other-tenant'},json=body).status_code==404
        assert c.post(path,json=body).status_code==401
        assert c.post(path+'?after=-1',headers=AUTH,json=body).status_code==422
        with db.transaction() as s:
            assert {table:s.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in before}==before
        # A stale receipt is not allowed to stream a formerly valid summary.
        updated=c.put(f'/api/v1/requests/{req["id"]}',headers=AUTH,json={
          'title':'Changed version','sku':'STAND-01','quantity':'20','budget':'30000',
          'max_delivery_days':14,'expected_version':req['version']})
        assert updated.status_code==200,updated.text
        stale=c.post(path,headers=AUTH,json=body)
        assert stale.status_code==200
        assert parse_sse(stale.text)[1][0]['snapshot']['current'] is False
        assert not any(e['type']=='TEXT_MESSAGE_CONTENT' for e,_ in parse_sse(stale.text))
