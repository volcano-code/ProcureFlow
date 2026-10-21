"""Grouped public data and partition accounting. None of these are model scores."""
import json
from pathlib import Path
import shutil
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from evals.candidate_quality import load_inputs,load_gold,grade,fingerprint
from evals.candidate_grouping import audit,corpus
from scripts.evaluate_candidates import run_suite


def copy_data(tmp_path):
    root=tmp_path/'evals'
    for name in ('candidate12','candidate20','candidate60'):shutil.copytree(ROOT/'evals'/name,root/name)
    return root/'candidate60'


def rewrite_manifest(root,manifest):
    (root/'manifest.json').write_text(json.dumps(manifest))
    for split in ('dev','validation','evaluation'):
        for name in ('inputs','gold'):
            p=root/split/(name+'.json');d=json.loads(p.read_text());d['manifest_sha256']=fingerprint(manifest);p.write_text(json.dumps(d))


def test_sixty_grouped_public_tasks_not_unseen_holdout():
    result=audit(ROOT/'evals/candidate60')
    assert result['tasks']==60 and result['partitions']=={'Dev':30,'Validation':10,'Evaluation':20}
    assert not result['independently_unseen'] and not result['gold_independently_reviewed']
    assert load_inputs(ROOT/'evals/candidate60/dev')['cases'][:20]==load_inputs(ROOT/'evals/candidate20')['cases']
    assert load_gold(ROOT/'evals/candidate60/dev')['cases'][:20]==load_gold(ROOT/'evals/candidate20')['cases']


@pytest.mark.parametrize('split,n',[('dev',30),('validation',10),('evaluation',20)])
def test_no_key_runs_remain_blocked_and_partition_label_preserved(split,n):
    path=ROOT/'evals/candidate60'/split;inputs=load_inputs(path)
    for strategy in ('single_shot','agent'):
        report=run_suite(inputs,mode='live',strategy=strategy)
        assert report['status']=='blocked' and report['known_provider_requests']==0
        assert len(report['cases'])==n
        assert grade(path,report)['split']==inputs['split']
        assert not grade(path,report)['model_quality_measured']


def test_input_loading_does_not_read_gold(tmp_path):
    root=copy_data(tmp_path)
    (root/'gold-additions.json').unlink()
    (root.parent/'candidate20/gold.json').unlink()
    (root.parent/'candidate12/gold.json').unlink()
    assert len(load_inputs(root/'evaluation')['cases'])==20
    with pytest.raises(OSError):load_gold(root/'evaluation')


@pytest.mark.parametrize('mutation',['group','duplicate_id','missing_id','partition_size','unseen_claim','input_hash','source_duplicate'])
def test_partition_integrity_rejects_leakage_and_forged_scope(tmp_path,mutation):
    root=copy_data(tmp_path);m=json.loads((root/'manifest.json').read_text())
    if mutation=='group':m['groups']['cq-031']=m['groups']['cq-021']
    elif mutation=='duplicate_id':m['partitions']['Validation'][0]='cq-021'
    elif mutation=='missing_id':m['groups'].pop('cq-031')
    elif mutation=='partition_size':m['partitions']['Evaluation'].pop()
    elif mutation=='unseen_claim':m['independently_unseen']=True
    elif mutation=='input_hash':m['input_additions_sha256']='0'*64
    else:
        p=root/'inputs-additions.json';data=json.loads(p.read_text());data['cases'][10]['text']=data['cases'][0]['text']
        p.write_text(json.dumps(data));m['input_additions_sha256']=fingerprint(data)
    rewrite_manifest(root,m)
    with pytest.raises(ValueError):audit(root)


def test_unknown_supplier_overlap_detected_even_with_distinct_group_labels(tmp_path):
    root=copy_data(tmp_path);p=root/'gold-additions.json';g=json.loads(p.read_text())
    g['cases'][10]['values']['supplier_id']=g['cases'][0]['values']['supplier_id'];p.write_text(json.dumps(g))
    m=json.loads((root/'manifest.json').read_text());m['gold_additions_sha256']=fingerprint(g);rewrite_manifest(root,m)
    with pytest.raises(ValueError,match='CROSS_SPLIT_SUPPLIER_LEAK'):audit(root)


def test_thirty_case_live_does_not_silently_raise_default_call_budget():
    inputs=load_inputs(ROOT/'evals/candidate60/dev')
    assert run_suite(inputs,mode='live')['budgets']['max_provider_requests']==80
    assert run_suite(inputs,mode='live',max_requests=120)['budgets']['max_provider_requests']==120
    with pytest.raises(ValueError):run_suite(inputs,mode='live',max_requests=121)
