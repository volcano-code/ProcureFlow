"""Offline adversarial checks; official ag-ui-protocol validation lives in CI."""
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from procureflow.agui_observer import ObserveInput, observation_events, event_projection, verify_watch
from procureflow.errors import DomainError


def run(status='RUNNING', current=True):
    return dict(id='adv_123', request_id='req_123', status=status, input_hash='abc',
                output={'summary': 'A grounded, read-only summary'}, current=current,
                error_code=None, stale_reason=None)


def payload(**kw):
    return {'threadId':'req_123','runId':'adv_123','messages':[], 'protocolVersion':'1.0',**kw}


def test_only_official_empty_1_0_observation_input_allowed():
    assert ObserveInput.model_validate(payload()).runId=='adv_123'
    for addition in ({'messages':[{'role':'user','content':'approve me'}]},
                     {'tools':[{'name':'execute_purchase'}]},{'context':['secret']},
                     {'state':{'tenantId':'other'}},{'resume':[{'interruptId':'x','status':'resolved'}]},
                     {'forwardedProps':{'url':'http://x'}},{'runId':'../other'},
                     {'protocolVersion':'0.1'}, {'extra':'ignored'}):
        with pytest.raises(ValidationError): ObserveInput.model_validate(payload(**addition))


def test_observation_lifecycle_explicitly_not_model_run_success():
    rows=[{'id':2,'type':'ADVICE_STARTED','payload':{'run_id':'adv_123','api_key':'NEVER-LEAK'}},
          {'id':7,'type':'AGENT_TOOL_STARTED','payload':{'run_id':'adv_123','call_id':'t1','tool':'get_comparison','prompt':'LEAK'}},
          {'id':9,'type':'ADVICE_COMPLETED','payload':{'run_id':'other','private':'LEAK'}}]
    events=list(observation_events(run(),rows,0))
    assert [event['type'] for event,cursor in events]==['RUN_STARTED','STATE_SNAPSHOT','CUSTOM','CUSTOM','RUN_FINISHED']
    assert events[1][0]['snapshot']['agentStatus']=='RUNNING'
    assert events[1][0]['snapshot']['humanAction']=='observe_only'
    assert events[-1][0]['result']=={'mode':'observation_only','agentStatus':'RUNNING','providerSuccessNotImplied':True}
    assert [cursor for event,cursor in events if cursor is not None]==[2,7]
    assert 'LEAK' not in str(events)


def test_completed_summary_is_plain_text_not_hidden_model_state():
    events=list(observation_events(run('COMPLETED'),[],0))
    assert [e['type'] for e,_ in events][-4:]==['TEXT_MESSAGE_START','TEXT_MESSAGE_CONTENT','TEXT_MESSAGE_END','RUN_FINISHED']
    assert events[-3][0]['delta']=='A grounded, read-only summary'
    stale=list(observation_events(run('COMPLETED',False),[],0))
    assert all(e['type']!='TEXT_MESSAGE_CONTENT' for e,_ in stale)
    huge=run('COMPLETED');huge['output']['summary']='x'*8001
    assert all(e['type']!='TEXT_MESSAGE_CONTENT' for e,_ in observation_events(huge,[],0))


def test_audit_allowlist_and_tool_progress_privacy():
    row={'id':4,'type':'AGENT_TOOL_COMPLETED','payload':{'run_id':'adv_123','tool':'search_policy','call_id':'id',
          'duration_ms':1.2,'raw_tool_output':'SECRET','authorization':'Bearer example'}}
    event=event_projection(row,'adv_123')
    assert event['value']['detail']=={'tool':'search_policy','call_id':'id','duration_ms':1.2}
    assert event_projection({'type':'APPROVAL_CREATED','payload':{'run_id':'adv_123'},'id':5},'adv_123') is None
    assert event_projection({'type':'ADVICE_RESERVED','payload':{'run_id':'other'},'id':6},'adv_123') is None
    assert event_projection({'type':'AGENT_TOOL_STARTED','payload':None,'id':6},'adv_123') is None


def test_session_scope_is_rechecked_and_mismatched_run_denied():
    class Advice:
        def __init__(self): self.calls=0
        def get(self,_p,_id): self.calls+=1;return run()
    class Procurement:
        def __init__(self):self.calls=[]
        def events(self,p,r,after):self.calls.append((r,after));return [{'id':1,'type':'ADVICE_RESERVED','payload':{'run_id':'adv_123'}}]
    a,p=Advice(),Procurement()
    dto,logs=verify_watch(None,'adv_123',ObserveInput.model_validate(payload()),a,p,0)
    assert dto['status']=='RUNNING' and len(logs)==1 and a.calls==2 and p.calls==[('req_123',0)]
    with pytest.raises(DomainError) as err:
        verify_watch(None,'adv_123',ObserveInput.model_validate(payload(threadId='req_other')),a,p,0)
    assert err.value.status_code==403
    class Changed(Advice):
        def get(self,_p,_id):
            self.calls+=1
            return run(status='COMPLETED' if self.calls>1 else 'RUNNING')
    with pytest.raises(DomainError) as err:
        verify_watch(None,'adv_123',ObserveInput.model_validate(payload()),Changed(),p,0)
    assert err.value.code=='AGUI_SNAPSHOT_CHANGED'


def test_reference_staleness_human_review_fence():
    for state in ('FAILED','INTERRUPTED','STALE'):
        snapshot=list(observation_events(run(state),[],0))[1][0]['snapshot']
        assert snapshot['humanAction']=='manual_reconcile_no_replay'
        assert snapshot['externalWritePermitted'] is False
    snapshot=list(observation_events(run('COMPLETED'),[],0))[1][0]['snapshot']
    assert snapshot['humanAction']=='review_advice'
