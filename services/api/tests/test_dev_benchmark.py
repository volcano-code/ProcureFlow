"""Grader regression: reject fabricated successes; real-model quality is not tested."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from evals.devbench import (BenchmarkError, Observation, Reference, SafetyCounts, TaskBundle,
    fingerprint, grade_all, grade_one, load_gold, load_inputs, read_json, reference_valid, value_equal, write_json)
from evals.baseline import run_baseline

SUITE = ROOT / "evals/dev12"


@pytest.fixture(scope="module")
def data():
    manifest, bundles = load_inputs(SUITE)
    gold = load_gold(SUITE, bundles)
    return manifest, bundles, gold


def observed(bundle, answer):
    return Observation(schema_version=1, task_id=bundle.task.id,
        input_sha256=bundle.input_sha256, runner="unit-test-reference-shape", attempt=1,
        execution_status="executed", outcome=answer.outcome, quotes=answer.quotes,
        selected_document_id=answer.selected_document_id, issues=answer.issues,
        policy_references=answer.policy_references, safety=SafetyCounts(erp_write_attempts=0,
            approval_attempts=0, cross_scope_reads=0, confirmed_field_edits=0),
        model_used=False, model_calls=0, model_usage=None, cost=None,
        human_field_confirmation="none", elapsed_seconds=0.0)


def first(data):
    _, bundles, gold = data
    return bundles[0], gold.answers[0], observed(bundles[0], gold.answers[0])


def test_frozen_dev_inputs_and_reference_shapes_have_no_holdout_claim(data):
    manifest, bundles, gold = data
    assert manifest.split == "Dev" and manifest.synthetic_only
    assert len(bundles) == 12 and sum(len(b.contents) for b in bundles) == 16
    assert len({b.task.family for b in bundles}) < 12  # shared layouts are not fake-independent families
    records = [observed(b, a) for b, a in zip(bundles, gold.answers)]
    result = grade_all(bundles, gold, records)
    assert result["counts"]["passed"] == 12
    assert result["semantic_support_rate"] is None
    assert result["human_annotation_independent"] is False
    assert result["model_cost"] is None and not result["model_used"]


@pytest.mark.parametrize("wrong", ["1138.69", "1138.71", "NaN", "Infinity", "1e10", "", None])
def test_wrong_or_missing_total_cannot_pass(data, wrong):
    b, a, o = first(data)
    raw = o.model_dump()
    raw["quotes"][0]["total"] = wrong
    assert not grade_one(b, a, Observation.model_validate(raw))["passed"]


@pytest.mark.parametrize("field,value", [("supplier_id", "OFFICE-AA"), ("sku", "CLIP-AA"),
    ("shipping_cost", "0"), ("unit_price", "99.00"), ("tax_mode", "included"), ("delivery_days", 8)])
def test_valid_source_location_does_not_certify_wrong_field(data, field, value):
    b, a, o = first(data)
    raw = o.model_dump(); raw["quotes"][0]["values"][field] = value
    result = grade_one(b, a, Observation.model_validate(raw))
    assert result["citations"]["submitted"] == result["citations"]["valid_locations"]
    assert not result["passed"] and "FIELD_VALUE_MISMATCH" in result["reasons"]


@pytest.mark.parametrize("key,value", [("sha256", "0" * 64), ("version", "2"),
    ("document_id", "other-workspace-secret"), ("start_line", 99), ("excerpt", "made up")])
def test_tampered_citation_fails_without_echoing_source(data, key, value):
    b, a, o = first(data)
    raw = o.model_dump(); ref = raw["quotes"][0]["references"]["unit_price"][0]
    ref[key] = value
    if key == "start_line":ref["end_line"] = value
    result = grade_one(b, a, Observation.model_validate(raw))
    assert not result["passed"]
    assert "REFERENCE_SPAN_MISMATCH_OR_MISSING" in result["reasons"]


def test_no_citations_is_not_vacuous_hundred_percent(data):
    b, a, o = first(data)
    raw = o.model_dump(); raw["quotes"][0]["references"] = {}
    altered = Observation.model_validate(raw)
    result = grade_one(b, a, altered)
    assert not result["passed"] and result["citations"]["submitted"] == 0
    aggregate = grade_all([b], type(data[2]).model_construct(answers=[a]), [altered])
    assert aggregate["citation_location_validity"]["rate"] is None


def test_unknown_turned_into_zero_fails(data):
    b, a = data[1][4], data[2].answers[4]
    o = observed(b, a).model_dump();o["quotes"][0]["values"]["shipping_cost"] = "0.00"
    assert not grade_one(b, a, Observation.model_validate(o))["passed"]


@pytest.mark.parametrize("field", ["erp_write_attempts", "approval_attempts", "cross_scope_reads", "confirmed_field_edits"])
def test_unsafe_action_cannot_hide_behind_correct_answer(data, field):
    b, a, o = first(data)
    raw=o.model_dump();raw["safety"][field]=1
    assert "FORBIDDEN_SIDE_EFFECT" in grade_one(b,a,Observation.model_validate(raw))["reasons"]


@pytest.mark.parametrize("edit", ["unsupported", "missing_quote", "extra_issue", "wrong_selection", "wrong_input_hash"])
def test_execution_and_context_are_part_of_strict_success(data, edit):
    b,a,o=first(data);raw=o.model_dump()
    if edit=='unsupported':raw['execution_status']='unsupported'
    if edit=='missing_quote':raw['quotes']=[]
    if edit=='extra_issue':raw['issues'].append('UNNECESSARY_CLARIFICATION')
    if edit=='wrong_selection':raw['selected_document_id']=None
    if edit=='wrong_input_hash':raw['input_sha256']='0'*64
    assert not grade_one(b,a,Observation.model_validate(raw))['passed']


def test_policy_reference_is_version_sensitive(data):
    b,a=data[1][8],data[2].answers[8]
    raw=observed(b,a).model_dump();raw['policy_references'][0]['version']='1'
    result=grade_one(b,a,Observation.model_validate(raw))
    assert not result['passed'] and 'POLICY_REFERENCE_MISMATCH' in result['reasons']


def test_empty_observations_keep_all_tasks_in_denominator(data):
    result=grade_all(data[1],data[2],[])
    assert result['counts']['failed']==12 and result['strict_success_rate']==0
    assert not result['execution_complete']


@pytest.mark.parametrize('variant',['duplicate','unknown','mixed_attempts','mixed_runners'])
def test_retries_and_different_runners_cannot_be_cherry_picked(data,variant):
    records=[observed(b,a) for b,a in zip(data[1],data[2].answers)]
    if variant=='duplicate':records.append(records[0])
    else:
        raw=records[0].model_dump()
        if variant=='unknown':raw['task_id']='not-in-dataset'
        if variant=='mixed_attempts':raw['attempt']=2
        if variant=='mixed_runners':raw['runner']='other-model'
        records[0]=Observation.model_validate(raw)
    with pytest.raises(BenchmarkError):grade_all(data[1],data[2],records)


def test_source_integrity_checks_survive_in_memory_content_change(data):
    b,a,o=first(data)
    damaged=TaskBundle(b.task, {**b.contents,'quote':b.contents['quote']+b'fake\n'})
    assert not reference_valid(o.quotes[0].references['unit_price'][0],damaged)


@pytest.mark.parametrize('payload',['{"a":1,"a":2}', '{"value":NaN}', '{"value":Infinity}', '{broken'])
def test_duplicate_keys_and_nonfinite_json_rejected(tmp_path,payload):
    p=tmp_path/'bad.json';p.write_text(payload)
    with pytest.raises(BenchmarkError):read_json(p)


def test_source_hash_and_path_escape_checks(data,tmp_path):
    import shutil
    shutil.copytree(SUITE,tmp_path/'suite')
    manifest=json.loads((tmp_path/'suite/tasks.json').read_text())
    p=tmp_path/'suite'/manifest['tasks'][0]['documents'][0]['path']
    p.write_text(p.read_text()+'changed\n')
    with pytest.raises(BenchmarkError,match='SOURCE_INTEGRITY_FAILED'):load_inputs(tmp_path/'suite')
    manifest['tasks'][0]['documents'][0]['path']='inputs/../../secret.txt'
    (tmp_path/'suite/tasks.json').write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):load_inputs(tmp_path/'suite')


def test_baseline_runs_without_gold_and_ignores_ambient_live_configuration(data,tmp_path,monkeypatch):
    import shutil
    shutil.copytree(SUITE,tmp_path/'suite');shutil.rmtree(tmp_path/'suite/gold')
    _,bundles=load_inputs(tmp_path/'suite')
    live=tmp_path/'user-business.sqlite3'
    monkeypatch.setenv('PF_DATABASE_URL',f'sqlite:///{live}')
    monkeypatch.setenv('PF_MODE','private');monkeypatch.setenv('PF_AUTH_TOKENS','bad json')
    monkeypatch.setenv('PF_ERP_MODE','erpnext');monkeypatch.setenv('ERP_BASE_URL','https://invalid.example')
    monkeypatch.setenv('ERP_API_KEY','NEVER_USE_THIS');monkeypatch.setenv('LLM_API_KEY','NEVER_USE_THIS')
    result=run_baseline(bundles[0])
    assert result.outcome=='ready_for_review' and not live.exists()
    assert not result.model_used and not any(result.safety.model_dump().values())


def test_actual_legacy_baseline_exposes_shortcomings_not_gold_replay(data):
    records=[run_baseline(b) for b in data[1]]
    score=grade_all(data[1],data[2],records)
    assert score['counts']=={'tasks':12,'observations':12,'passed':7,'failed':5,'unsupported':2}
    # A deliberately frozen baseline; a new model runner must not be folded into A.
    assert [r['task_id'] for r in score['rows'] if not r['passed']]==[
        'pf-dev-003','pf-dev-004','pf-dev-008','pf-dev-009','pf-dev-010']


@pytest.mark.parametrize('payload',[True,1.2,'0'])
def test_safety_counts_are_strict_integers(payload):
    with pytest.raises(ValidationError):
        SafetyCounts(erp_write_attempts=payload,approval_attempts=0,cross_scope_reads=0,confirmed_field_edits=0)


def test_report_write_never_overwrites_previous_attempt(tmp_path):
    p=tmp_path/'report.json';write_json(p,{'old':True})
    with pytest.raises(FileExistsError):write_json(p,{'old':False})
    assert read_json(p)=={'old':True}


def test_oversized_json_is_rejected(tmp_path):
    p=tmp_path/'huge.json';p.write_bytes(b' '*(1024*1024+1))
    with pytest.raises(BenchmarkError,match='RECORD_TOO_LARGE'):read_json(p)


def test_grading_cli_strict_exit_and_missing_records(data,tmp_path):
    manifest,bundles,gold=data
    records=[run_baseline(b).model_dump(mode='json') for b in bundles]
    pred=tmp_path/'predictions.json'
    pred.write_text(json.dumps({'schema_version':1,'input_manifest_sha256':fingerprint(manifest.model_dump(mode='json')),
        'implementation_sha256':__import__('scripts.evaluate_dev',fromlist=['implementation_fingerprint']).implementation_fingerprint(),'observations':records}))
    common=[sys.executable,str(ROOT/'scripts/evaluate_dev.py'),'grade','--observations',str(pred)]
    strict=subprocess.run(common+['--output',str(tmp_path/'strict.json'),'--require-all-success'],capture_output=True,text=True,timeout=15)
    assert strict.returncode==1 and read_json(tmp_path/'strict.json')['counts']['failed']==5
    measurement=subprocess.run(common+['--output',str(tmp_path/'measurement.json')],capture_output=True,text=True,timeout=15)
    assert measurement.returncode==0 and not read_json(tmp_path/'measurement.json')['all_tasks_passed']
    raw=read_json(pred);raw['observations']=[];pred.write_text(json.dumps(raw))
    missing=subprocess.run(common+['--output',str(tmp_path/'missing.json')],capture_output=True,text=True,timeout=15)
    assert missing.returncode==1 and read_json(tmp_path/'missing.json')['counts']['failed']==12


@pytest.mark.parametrize('action', ['approve', 'enqueue', 'process_operation', 'edit_quote'])
def test_baseline_harness_blocks_business_authority_even_if_called(data, monkeypatch, action):
    from procureflow.service import ProcurementService
    def malicious_analyze(self, *args, **kwargs):
        return getattr(self, action)()
    monkeypatch.setattr(ProcurementService, 'analyze', malicious_analyze)
    with pytest.raises(RuntimeError, match='OFFLINE_EVALUATION_FORBIDS_'):
        run_baseline(data[1][0])


def test_stale_implementation_cannot_be_graded_as_current(data, tmp_path):
    manifest, bundles, _ = data
    record = run_baseline(bundles[0]).model_dump(mode='json')
    pred = tmp_path/'stale.json'
    pred.write_text(json.dumps({'schema_version':1,
        'input_manifest_sha256':fingerprint(manifest.model_dump(mode='json')),
        'implementation_sha256':'0'*64, 'observations':[record]}))
    p = subprocess.run([sys.executable, str(ROOT/'scripts/evaluate_dev.py'), 'grade',
         '--observations', str(pred), '--output', str(tmp_path/'report.json')],
         capture_output=True, text=True, timeout=15)
    assert p.returncode == 1 and not (tmp_path/'report.json').exists()
