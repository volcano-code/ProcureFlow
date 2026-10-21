"""Joint gate's fail-closed records and actual loopback fixture; not live LLM tests."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from check_candidate_procurement import check,STEPS


def records():
    ops=[{'scenario':name,'operation_id':'op_'+str(i),'candidate_run_id':'can_'+str(i),
        'snapshot_hash':str(i)*64,'remote_id':None if i==2 else 'PUR-SQTN-2026-0000'+str(i+1),
        'final_status':'NEEDS_HUMAN' if i==2 else 'COMPLETED','expected_total':'2000.00'}
        for i,name in enumerate(['normal','lost-receipt','revoked'])]
    run={'schema':'candidate-procurement-v1','status':'passed','steps':STEPS.copy(),'operations':ops,
        'synthetic_only':True,'scripted_provider':True,'live_erp':True,'real_model_used':False,
        'browser_responses_not_mocked':True,'human_approval_measured':False,'api_without_model_key':True,
        'candidate_worker_without_erp_key':True,'database':'postgresql','source_tree':'a'*40,
        'provider_requests':6,'erp_post_attempts':2,'business_audit':{'status':'passed','database':'postgresql',
        'candidate_runs':4,'imported':3,'cancelled':1,'quotes':3,'confirmed_quotes':3,'approvals':3,
        'operations':3,'completed':2,'denied':1,'done_outbox':3,'verified_receipts':2,
        'candidate_provenance_checked':True,'after_api_restart':True,'read_only_audit':True}}
    audit={'status':'passed','stage':'database-audit','source_tree':'a'*40,
        'operation_ids':[o['operation_id'] for o in ops[:2]],'draft_count':2,'purchase_order_count':0,
        'submitted_count':0,'contents_checked':True,'unique_index_checked':True,
        'permissions_checked':True,'revoked_operation_absent':True}
    return run,audit


def write(tmp_path,run,audit):
    for name,data in [('roundtrip',run),('database-audit',audit)]:
        (tmp_path/(name+'.json')).write_text(json.dumps(data))


def test_complete_record_has_narrow_scope(tmp_path):
    write(tmp_path,*records()); result=check(tmp_path)
    assert result['status']=='passed' and not result['real_model_used'] and result['real_erp_used']


@pytest.mark.parametrize('location,value',[
    ('status','running'),('database','sqlite'),('real_model_used',True),('scripted_provider',False),
    ('provider_requests',7),('provider_requests',True),('erp_post_attempts',3),('source_tree','bad'),
    ('human_approval_measured',True),('browser_responses_not_mocked',False),('api_without_model_key',False),
    ('candidate_worker_without_erp_key',False),('steps',STEPS[:-1])])
def test_reject_run_misclaims(tmp_path,location,value):
    run,audit=records();run[location]=value;write(tmp_path,run,audit)
    with pytest.raises(ValueError):check(tmp_path)


@pytest.mark.parametrize('key', ['candidate_runs','imported','cancelled','quotes','confirmed_quotes','approvals',
    'operations','completed','denied','done_outbox','verified_receipts'])
def test_reject_wrong_pg_counts(tmp_path,key):
    run,audit=records();run['business_audit'][key]+=1;write(tmp_path,run,audit)
    with pytest.raises(ValueError):check(tmp_path)


@pytest.mark.parametrize('key,value',[('source_tree','b'*40),('operation_ids',[]),('draft_count',3),
    ('purchase_order_count',1),('submitted_count',1),('permissions_checked',False),('revoked_operation_absent',False)])
def test_reject_bad_erp_audit(tmp_path,key,value):
    run,audit=records();audit[key]=value;write(tmp_path,run,audit)
    with pytest.raises(ValueError):check(tmp_path)


def test_reject_missing_audit_and_duplicate_json(tmp_path):
    write(tmp_path,*records());(tmp_path/'database-audit.json').unlink()
    with pytest.raises(OSError):check(tmp_path)
    (tmp_path/'database-audit.json').write_text('{"status":"passed","status":"passed"}')
    with pytest.raises(ValueError):check(tmp_path)


def test_fixture_uses_actual_bound_source_but_only_fixed_synthetic_values():
    import httpx
    from verify_candidate_procurement import provider
    from fixtures.candidate_provider import KEY,MODEL
    data={'company':'ProcureFlow Sandbox','sku':'PF-SANDBOX-ITEM','supplier':'PF Synthetic Supplier'}
    with provider(data) as (url,source,state):
        client=httpx.Client(base_url=url,trust_env=False)
        r=client.post('/chat/completions',headers={'Authorization':'Bearer '+KEY},json={
            'model':MODEL,'messages':[{'role':'user','content':'synthetic'}]})
        assert r.status_code==200 and state['calls']==1
        fragments=[{'id':'e'+str(i),'text':line} for i,line in enumerate(source.splitlines())]
        r=client.post('/chat/completions',headers={'Authorization':'Bearer '+KEY},json={
            'model':MODEL,'messages':[{'role':'tool','content':json.dumps({'data':{'fragments':fragments}})}]})
        assert r.status_code==200
        result=json.loads(r.json()['choices'][0]['message']['content'])
        assert result['values']['supplier_id']==data['supplier'] and result['values']['sku']==data['sku']
        assert len(result['references'])==11 and state['calls']==2
        assert client.post('/other').status_code==403
        client.close()
    with pytest.raises(ValueError):
        with provider({**data,'company':'production'}):pass
