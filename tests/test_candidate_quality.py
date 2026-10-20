"""Scorer/security tests. Gold-shaped self-checks are NOT model performance."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.candidate_quality import (FIELDS, compare, fingerprint, grade, load_inputs, load_json,
    seal_fields, validate_record, value_digest)
from evals.candidate_trial import empty_record
from scripts import evaluate_candidates as runner
SUITE = ROOT / 'evals/candidate12'


def reports(mode='baseline'):
    inputs = load_inputs(SUITE); gold = load_json(SUITE / 'gold.json')
    records = []
    for case, answer in zip(inputs['cases'], gold['cases']):
        record = empty_record(case, mode)
        evidence = {field: [{'line': n,
            'text_sha256': hashlib.sha256(case['text'].splitlines()[n-1].encode()).hexdigest(),
            'source_sha256': case['sha256']} for n in lines] for field, lines in answer['support_lines'].items()}
        record.update(status='observed', reason=None, fields=seal_fields(answer['values'], evidence),
            clarifications=answer['clarifications'], side_effects=dict(quotes=0, approvals=0, operations=0, erp_attempts=0))
        if mode == 'live':
            record.update(network_attempted=True, provider_requests=2,
                          usage=dict(prompt_tokens=10, completion_tokens=10, total_tokens=20))
        records.append(record)
    return {'schema_version': 1, 'mode': mode, 'attempt': 1,
            'implementation_sha256': 'a'*64, 'input_sha256': fingerprint(inputs), 'cases': records}


def test_gold_shaped_self_check_not_model_result():
    scored = grade(SUITE, reports())
    assert scored['counts'] == dict(tasks=12, observed=12, passed=12, not_passed=0)
    assert scored['model_quality_measured'] is False
    assert scored['metrics']['semantic_support_rate'] is None


@pytest.mark.parametrize('field', FIELDS)
def test_every_known_field_is_scored(field):
    r = reports(); r['cases'][0]['fields'][field]['value_sha256'] = 'b'*64
    assert grade(SUITE, r)['counts']['passed'] == 11


@pytest.mark.parametrize('change', ['zero_unknown', 'wrong_source', 'wrong_line', 'wrong_excerpt',
                                  'no_citation', 'extra_clarification', 'no_clarification', 'side_effect'])
def test_semantic_and_side_effect_counterexamples(change):
    r = reports(); field = r['cases'][0]['fields']['unit_price']
    if change == 'zero_unknown':
        r['cases'][6]['fields']['shipping_cost'].update(known=True, value_sha256=value_digest('shipping_cost', '0'))
    elif change == 'wrong_source': field['references'][0]['source_sha256'] = '0'*64
    elif change == 'wrong_line': field['references'][0]['line'] = 2
    elif change == 'wrong_excerpt': field['references'][0]['text_sha256'] = '0'*64
    elif change == 'no_citation': field['references'] = []
    elif change == 'extra_clarification': r['cases'][0]['clarifications'] = {'unit_price': 'missing'}
    elif change == 'no_clarification': r['cases'][6]['clarifications'] = {}
    else: r['cases'][0]['side_effects']['operations'] = 1
    assert grade(SUITE, r)['counts']['passed'] < 12


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'reordered', 'input_hash', 'implementation',
                                  'mixed_mode', 'nan_duration', 'boolean_counter', 'raw_string', 'float_money'])
def test_malformed_receipts_fail_closed(change):
    r = reports()
    if change == 'missing': r['cases'].pop()
    elif change == 'duplicate': r['cases'][1] = r['cases'][0]
    elif change == 'reordered': r['cases'].reverse()
    elif change == 'input_hash': r['input_sha256'] = '0'*64
    elif change == 'implementation': r['implementation_sha256'] = None
    elif change == 'mixed_mode': r['cases'][0]['mode'] = 'fixture'
    elif change == 'nan_duration': r['cases'][0]['elapsed_seconds'] = float('nan')
    elif change == 'boolean_counter': r['cases'][0]['provider_requests'] = True
    elif change == 'raw_string': r['cases'][0]['fields']['supplier_id']['secret'] = 'CANARY-DO-NOT-EXPORT'
    else:
        with pytest.raises(ValueError): value_digest('unit_price', 2.0)
        return
    with pytest.raises((ValueError, TypeError)):
        grade(SUITE, r)


def test_only_explicit_live_can_be_compared():
    with pytest.raises(ValueError): compare(SUITE, reports(), reports('fixture'))
    r = reports('live'); r['cases'][0] = empty_record(load_inputs(SUITE)['cases'][0], 'live')
    assert compare(SUITE, reports(), r)['paired_net_improvement'] is None


def test_duplicate_keys_rejected(tmp_path):
    p = tmp_path/'r.json'; p.write_text('{"cases":[],"cases":[1]}')
    with pytest.raises(ValueError): load_json(p)


def test_opaque_identifier_case_not_folded_and_decimal_normalized():
    assert value_digest('supplier_id', 'Acme') != value_digest('supplier_id', 'ACME')
    assert value_digest('unit_price', '2.00') == value_digest('unit_price', '2')


def test_public_receipts_do_not_export_strings():
    values = load_json(SUITE/'gold.json')['cases'][0]['values'] | {'supplier_id': 'SECRET-REFLECTION-CANARY'}
    assert 'SECRET-REFLECTION-CANARY' not in json.dumps(seal_fields(values, {}))


def test_no_optin_does_not_even_read_credentials():
    class Forbidden(dict):
        def get(self, *a): raise AssertionError('credential read')
    assert runner.live_config(False, Forbidden())[1] == 'EXPLICIT_NETWORK_OPT_IN_REQUIRED'


@pytest.mark.parametrize('url,host', [
    ('http://api.example.com', 'api.example.com'), ('https://127.0.0.1', '127.0.0.1'),
    ('https://api.example.com?q=secret', 'api.example.com'), ('https://x:secret@api.example.com', 'api.example.com'),
    ('https://different.example.com', 'api.example.com'), ('https://fixture.invalid', 'fixture.invalid'),
    ('https://api.example.com:444', 'api.example.com'), ('https://localhost', 'localhost')])
def test_live_endpoint_requires_exact_approved_https_host(url, host):
    cfg, reason = runner.live_config(True, dict(LLM_BASE_URL=url, PF_EVAL_APPROVED_HOST=host,
        LLM_API_KEY='secret', LLM_MODEL='configured-model'))
    assert cfg is None and reason == 'MODEL_CONFIGURATION_INVALID'


def test_live_missing_config_never_constructs_worker(monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    r = runner.run_suite(load_inputs(SUITE), mode='live', allow_network=True,
                        execute=lambda *a: pytest.fail('must not execute'))
    assert r['status'] == 'blocked'
    assert len(r['cases']) == 12 and all(x['network_attempted'] is False for x in r['cases'])
    assert grade(SUITE, r)['counts']['passed'] == 0


def test_ambiguous_failure_stops_live_calls(monkeypatch):
    monkeypatch.setattr(runner, 'live_config', lambda *a: (dict(base_url='https://api.example.com',
        api_key='secret', model='example', thinking_mode='default'), None))
    seen=[]
    def execute(case, config):
        seen.append(case['id']); r=empty_record(case, 'live')
        r.update(status='failed', reason='MODEL_TIMEOUT', provider_requests=None, network_attempted=None)
        return r
    r = runner.run_suite(load_inputs(SUITE), mode='live', allow_network=True, execute=execute)
    assert len(seen) == 1 and r['status'] == 'partial'
    assert r['cases'][0]['usage'] is None and r['cases'][0]['cost'] is None


def test_attempt_budget_reserves_worst_case():
    count=[]
    def execute(case, config):
        count.append(1); r=empty_record(case,'fixture');r.update(status='failed',provider_requests=2)
        return r
    r = runner.run_suite(load_inputs(SUITE), mode='fixture', max_requests=4, execute=execute)
    assert len(count) == 1 and r['known_provider_requests'] == 2


def test_child_environment_is_allowlisted(monkeypatch):
    monkeypatch.setenv('ERP_API_SECRET','CANARY'); monkeypatch.setenv('LLM_API_KEY','CANARY')
    env = runner.child_environment('/tmp/synthetic')
    assert 'ERP_API_SECRET' not in env and 'LLM_API_KEY' not in env and 'CANARY' not in json.dumps(env)


def test_existing_output_prevents_live_attempt(tmp_path):
    p=tmp_path/'result.json';p.write_text('existing evidence')
    code=runner.main(['run','--mode','live','--allow-network','--output',str(p)])
    assert code == 1 and p.read_text() == 'existing evidence'


def test_runtime_baseline_uses_actual_parser_not_gold():
    inputs=load_inputs(SUITE)
    cfg=dict(mode='baseline',allow_network=False,approved_host='',max_calls=4,wall_seconds=45,
        token_budget=16000,base_url='https://fixture.invalid',api_key='synthetic-fixture',
        model='scripted-fixture',thinking_mode='default')
    case=inputs['cases'][0]
    record=runner.execute_case(case,cfg)
    assert record['status']=='observed' and record['provider_requests']==0
    assert record['fields']['unit_price']['value_sha256']==value_digest('unit_price','20')
    # Same runner, modified input: cannot pull an answer by task ID.
    changed=case|{'text':case['text'].replace('unit_price: 20.00','unit_price: 29.00')}
    changed['sha256']=hashlib.sha256(changed['text'].encode()).hexdigest()
    assert runner.execute_case(changed,cfg)['fields']['unit_price']['value_sha256']==value_digest('unit_price','29')


def test_runtime_model_fixture_uses_actual_candidate_queue(monkeypatch):
    # Only the synthetic fixture is observed here; preserve safe failure category
    # without exposing model prose or weakening receipt validation.
    original = runner.subprocess.run
    diagnostics = []
    def inspect_worker(*args, **kwargs):
        response = original(*args, **kwargs)
        if args and args[0][-1] == '--_worker':
            try:
                payload = json.loads(response.stdout)
                case = load_inputs(SUITE)['cases'][0]
                validate_record(payload, case, 'fixture')
                diagnostics.append({'returncode': response.returncode, 'valid_receipt': True,
                                    'status': payload['status'], 'reason': payload['reason']})
            except (ValueError, TypeError, KeyError) as error:
                # Only locally defined validation codes, never arbitrary error text.
                safe_codes = {'INVALID_RECORD_KEYS', 'INVALID_RECORD_IDENTITY', 'INVALID_RECORD_STATUS',
                    'INVALID_ACCOUNTING', 'INVALID_REQUEST_COUNT', 'INVALID_DURATION', 'INVALID_TOOL_PATH',
                    'INVALID_USAGE', 'INVALID_FIELDS', 'INCOMPLETE_OBSERVED_FIELDS', 'INVALID_FIELD_OBSERVATION',
                    'INVALID_REFERENCES', 'INVALID_LOCATOR', 'INVALID_CLARIFICATIONS', 'INVALID_SIDE_EFFECTS',
                    'OFFLINE_MODE_NETWORK_MISMATCH', 'BASELINE_MODEL_CALL'}
                diagnostics.append({'returncode': response.returncode,
                    'validation_code': str(error) if str(error) in safe_codes else 'MALFORMED_WORKER_JSON'})
        return response
    monkeypatch.setattr(runner.subprocess, 'run', inspect_worker)
    r=runner.run_suite(load_inputs(SUITE),mode='fixture',limit=1,max_requests=4)
    case=r['cases'][0]
    assert case['status']=='observed', diagnostics
    assert case['provider_requests']==2 and case['network_attempted'] is False
    assert case['tool_path']==['get_request','get_evidence']
    assert case['side_effects']==dict(quotes=0,approvals=0,operations=0,erp_attempts=0)
    assert grade(SUITE,r)['model_quality_measured'] is False


def test_direct_worker_cannot_implicitly_enable_network(tmp_path):
    case=load_inputs(SUITE)['cases'][0]
    cfg=dict(mode='live',allow_network=False,directory=str(tmp_path),max_calls=4,wall_seconds=45,
        token_budget=16000,base_url='https://api.example.com',api_key='secret',
        model='test-model',approved_host='api.example.com',thinking_mode='default')
    p=subprocess.run([sys.executable,str(ROOT/'scripts/evaluate_candidates.py'),'--_worker'],
        input=json.dumps(dict(case=case,config=cfg)),text=True,capture_output=True,timeout=10)
    assert json.loads(p.stdout)['reason']=='INVALID_WORKER_CONFIGURATION'
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('mode', ['baseline', 'fixture', 'live'])
def test_unexpected_failure_preserves_network_scope_without_echoing_error(monkeypatch, mode):
    from evals import candidate_trial
    def fail(*args):
        raise RuntimeError('PRIVATE-CANARY-MUST-NOT-LEAK')
    monkeypatch.setattr(candidate_trial, 'baseline', fail)
    monkeypatch.setattr(candidate_trial, 'candidate', fail)
    case=load_inputs(SUITE)['cases'][0]
    result=candidate_trial.run(case, {'mode': mode})
    assert result['status']=='failed' and result['reason']=='TRIAL_FAILED'
    assert result['network_attempted'] is (None if mode == 'live' else False)
    assert result['provider_requests'] is (0 if mode == 'baseline' else None)
    assert 'PRIVATE-CANARY' not in json.dumps(result)
    validate_record(result, case, mode)
