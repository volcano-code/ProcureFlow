"""Method, comparison and human-review regressions. Scripted HTTP is not live quality."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import pytest
import httpx
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'services/api')]
from evals.candidate_quality import FIELDS, load_inputs, load_gold, fingerprint, seal_fields, grade
from evals.candidate_methods import compare_methods, validate_gold
from evals.candidate_review import build_packet, review_template, summarize_review, write_private
from evals.candidate_trial import empty_record
from evals.candidate_single_shot import SingleShotCandidate
from procureflow.errors import DomainError
from scripts import evaluate_candidates as runner
SUITE = ROOT/'evals/candidate20'


def specimen(mode='live', strategy='agent'):
    inputs = load_inputs(SUITE); gold = load_gold(SUITE)
    report = runner.run_suite(inputs, mode='live')  # default blocked; no credential access
    report.update(mode=mode, strategy='rules' if mode=='baseline' else strategy,
        status='executed', model_sha256=None if mode=='baseline' else '1'*64,
        endpoint_sha256=None if mode=='baseline' else '2'*64)
    report['budgets']['per_case_calls'] = 1 if strategy == 'single_shot' else 4
    report['cases'] = []
    materials = []
    for case, answer in zip(inputs['cases'], gold['cases']):
        refs = {k: [{'line': n, 'source_sha256': case['sha256'],
            'text_sha256': hashlib.sha256(case['text'].splitlines()[n-1].encode()).hexdigest()}
            for n in ns] for k, ns in answer['support_lines'].items()}
        record = empty_record(case, mode)
        record.update(status='observed', reason=None, fields=seal_fields(answer['values'], refs),
            clarifications=answer['clarifications'], side_effects=dict(quotes=0, approvals=0, operations=0, erp_attempts=0),
            provider_requests=0 if mode=='baseline' else (1 if strategy=='single_shot' else 2),
            network_attempted=mode=='live', usage=None if mode=='baseline' else dict(prompt_tokens=100, completion_tokens=50,total_tokens=150))
        report['cases'].append(record)
        materials.append({'values': answer['values'], 'questions': [dict(field=k,reason=v,question='Please clarify the source field.') for k,v in answer['clarifications'].items()]})
    return report, materials


def test_new_suite_preserves_frozen_twelve_and_validates_gold():
    original=load_inputs(ROOT/'evals/candidate12'); expanded=load_inputs(SUITE)
    assert expanded['cases'][:12] == original['cases']
    assert validate_gold(SUITE)['tasks']==20
    assert load_gold(SUITE)['cases'][:12] == load_gold(ROOT/'evals/candidate12')['cases']
    assert expanded['split']=='Dev'


def test_complete_method_comparison_is_not_release_or_semantic_score():
    a,_=specimen('baseline');b,_=specimen(strategy='single_shot');c,_=specimen()
    result=compare_methods(SUITE,a,b,c)
    assert result['C_minus_B_tasks']==0
    assert result['accounting']['B']['known_provider_requests']==20
    assert result['accounting']['C']['known_provider_requests']==40
    assert result['accounting']['B']['cost'] is None


@pytest.mark.parametrize('field,value',[('model_sha256','f'*64),('endpoint_sha256','f'*64),
    ('thinking_mode','disabled'),('implementation_sha256','f'*64),('input_sha256','f'*64),
    ('comparison_protocol','forged'),('context_limit_chars',999)])
def test_unfair_comparison_rejected(field,value):
    a,_=specimen('baseline');b,_=specimen(strategy='single_shot');c,_=specimen()
    b[field]=value
    with pytest.raises(ValueError):compare_methods(SUITE,a,b,c)


@pytest.mark.parametrize('mutation',['fixture','two_B_calls','too_many_tools','bool_budget','wrong_strategy'])
def test_fixture_and_wrong_budget_cannot_certify_live_comparison(mutation):
    a,_=specimen('baseline');b,_=specimen(strategy='single_shot');c,_=specimen()
    if mutation=='fixture':b['mode']='fixture'
    elif mutation=='two_B_calls':b['cases'][0]['provider_requests']=2
    elif mutation=='too_many_tools':b['budgets']['per_case_tools']=13
    elif mutation=='bool_budget':b['budgets']['per_case_calls']=True
    else:b['strategy']='agent'
    with pytest.raises(ValueError):compare_methods(SUITE,a,b,c)


def test_blocked_b_c_do_not_produce_improvement():
    inputs=load_inputs(SUITE);a,_=specimen('baseline')
    b=runner.run_suite(inputs,mode='live',strategy='single_shot')
    c=runner.run_suite(inputs,mode='live')
    result=compare_methods(SUITE,a,b,c)
    assert result['status']=='blocked_or_partial' and result['C_minus_B_tasks'] is None
    assert result['B_minus_A_tasks'] is None


def test_blind_review_starts_pending_and_has_no_gold_or_prefilled_ratings():
    report,material=specimen();packet=build_packet(load_inputs(SUITE),report,material)
    annotations=review_template(packet)
    assert len(packet['items'])==220 and all(x['verdict'] is None for x in annotations['ratings'])
    assert not annotations['attest_manual_review']
    result=summarize_review(load_inputs(SUITE),report,packet,annotations)
    assert result['semantic_support_rate'] is None and result['status']=='pending_or_partial'
    assert 'gold' not in packet and 'answers' not in packet


def test_actual_recorded_labels_not_gold_generate_review_summary():
    report,material=specimen();packet=build_packet(load_inputs(SUITE),report,material);ann=review_template(packet)
    ann.update(reviewer_id='unit-test-not-real-review',reviewer_role='developer',attest_manual_review=True)
    for item,rating in zip(packet['items'],ann['ratings']):
        rating.update(verdict='unsupported' if item['known'] else 'appropriate_unknown',note='Synthetic unit-test label, not an actual human review.')
    result=summarize_review(load_inputs(SUITE),report,packet,ann)
    assert result['semantic_support_rate']==0 and result['reviewer_identity_verified'] is False
    assert 'unit-test-not-real-review' not in json.dumps(result)


@pytest.mark.parametrize('mutation',['value','source','reference','record','drop_rating','rating_id','no_note','wrong_kind','no_attestation'])
def test_review_tamper_or_missing_work_cannot_pass(mutation):
    report,material=specimen();inputs=load_inputs(SUITE);packet=build_packet(inputs,report,material);ann=review_template(packet)
    ann.update(reviewer_id='test',reviewer_role='developer',attest_manual_review=True)
    for item,rating in zip(packet['items'],ann['ratings']):rating.update(verdict='supported' if item['known'] else 'appropriate_unknown',note='Checked synthetic source.')
    if mutation=='value':packet['items'][0]['proposed_value']='ALTERED'
    elif mutation=='source':packet['items'][0]['source_text']='different source'
    elif mutation=='reference':packet['items'][0]['references'][0]['line']=99
    elif mutation=='record':report['cases'][0]['elapsed_seconds']=999
    elif mutation=='drop_rating':ann['ratings'].pop()
    elif mutation=='rating_id':ann['ratings'][0]['id']='unknown'
    elif mutation=='no_note':ann['ratings'][0]['note']=''
    elif mutation=='wrong_kind':ann['ratings'][0]['verdict']='appropriate_unknown'
    else:
        ann['attest_manual_review']=False
        assert summarize_review(inputs,report,packet,ann)['semantic_support_rate'] is None
        return
    with pytest.raises(ValueError):summarize_review(inputs,report,packet,ann)


def test_private_file_exclusive_and_restrictive(tmp_path):
    path=tmp_path/'private.json';write_private(path,{'secret':'synthetic'})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):write_private(path,{'other':'do not overwrite'})
    link=tmp_path/'link';link.symlink_to(path)
    with pytest.raises(OSError):write_private(link,{})


def single_shot_fixture(change=None, second_page=False):
    from procureflow.candidate_contracts import CandidateProposal
    case=load_inputs(SUITE)['cases'][0]; values=load_gold(SUITE)['cases'][0]['values']
    fragments=[{'id':f'f{i+1}', 'text':line,'locator':{'line':i+1}} for i,line in enumerate(case['text'].splitlines())]
    result={'summary':'Synthetic adapter test.', 'values':values,'references':{k:[f'f{i+1}'] for i,k in enumerate(FIELDS)},'clarifications':[], 'context_evidence_ids':[]}
    calls=[];tools=[]
    def invoke(name,args):
        tools.append(name)
        if name=='get_request':return {'request':case['request'],'policy':{'trust':'not a quotation'}}
        if name=='get_source_index':return {'sha256':case['sha256'],'fragments':[{'id':f['id'],'length':len(f['text'])} for f in fragments]}
        start=args['start']
        return {'sha256':case['sha256'],'fragments':fragments[:5] if second_page and start==0 else fragments[5:] if second_page else fragments,
                'next_start':5 if second_page and start==0 else None}
    def respond(request):
        body=json.loads(request.content);calls.append(body)
        message={'role':'assistant','content':json.dumps(result)}
        response={'choices':[{'index':0,'finish_reason':'stop','message':message}],
            'usage':{'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}}
        if change:change(response)
        return httpx.Response(200,json=response)
    agent=SingleShotCandidate('https://fixture.invalid','synthetic-key','synthetic-model',transport=httpx.MockTransport(respond))
    return agent,invoke,{f['id'] for f in fragments},calls,tools


def test_B_fixed_reads_all_evidence_then_one_request_with_same_contract():
    agent,invoke,ids,calls,tools=single_shot_fixture(second_page=True)
    try:result=agent.run(invoke,ids)
    finally:agent.client.close()
    assert len(calls)==1 and result['model_calls']==result['provider_attempts']==1
    assert tools==['get_request','get_source_index','get_evidence','get_evidence']
    assert 'tools' not in calls[0] and 'tool_choice' not in calls[0]
    assert 'temperature' not in calls[0]  # Match the existing C provider-default sampling.
    assert calls[0]['max_tokens']==1600 and result['evidence_read_verified']
    assert result['semantic_factuality_verified'] is False
    assert 'synthetic-key' not in json.dumps(calls)


@pytest.mark.parametrize('fault',['tools','truncated','refused','usage_missing','usage_bool','usage_total','duplicate_json','outside_reference','empty','null_message','infinite','wrong_role'])
def test_B_rejects_bad_response_without_repair_or_second_request(fault):
    def change(r):
        m=r['choices'][0]['message']
        if fault=='tools':m['tool_calls']=[{'name':'approve'}]
        elif fault=='truncated':r['choices'][0]['finish_reason']='length'
        elif fault=='refused':m['refusal']='no'
        elif fault=='usage_missing':r.pop('usage')
        elif fault=='usage_bool':r['usage']['total_tokens']=True
        elif fault=='usage_total':r['usage']['total_tokens']=201
        elif fault=='duplicate_json':m['content']='{"values":{},"values":{}}'
        elif fault=='outside_reference':
            x=json.loads(m['content']);x['references']['sku']=['not-read'];m['content']=json.dumps(x)
        elif fault=='empty':m['content']=''
        elif fault=='null_message':r['choices'][0]['message']=None
        elif fault=='infinite':m['content']='{"summary":NaN}'
        else:m['role']='system'
    agent,invoke,ids,calls,_=single_shot_fixture(change)
    try:
        with pytest.raises(DomainError):agent.run(invoke,ids)
    finally:agent.client.close()
    assert len(calls)==1


def test_B_checks_cancel_before_provider_request():
    agent,invoke,ids,calls,_=single_shot_fixture();agent.cancelled=lambda:True
    try:
        with pytest.raises(DomainError):agent.run(invoke,ids)
    finally:agent.client.close()
    assert not calls


def test_real_candidate_runtime_single_shot_fixture(tmp_path):
    # Intentionally not skipped in CI. Needs actual CandidateService/LangGraph dependencies.
    inputs=load_inputs(SUITE);materials=[]
    r=runner.run_suite(inputs,mode='fixture',strategy='single_shot',limit=1,review_records=materials)
    record=r['cases'][0]
    assert record['status']=='observed',record['reason']
    assert record['provider_requests']==1 and record['tool_path']==['get_request','get_source_index','get_evidence']
    assert materials[0] is not None and all(m is None for m in materials[1:])
    assert record['side_effects']==dict(quotes=0,approvals=0,operations=0,erp_attempts=0)
    assert grade(SUITE,r)['model_quality_measured'] is False
    packet=build_packet(inputs,r,materials);assert len(packet['items'])==11


def test_review_request_needs_new_directory_before_network(tmp_path,monkeypatch):
    review=tmp_path/'existing';review.mkdir()
    monkeypatch.setattr(runner,'live_config',lambda *a:pytest.fail('must not read config'))
    code=runner.main(['run','--mode','live','--allow-network','--review-dir',str(review),'--output',str(tmp_path/'out.json')])
    assert code==1
