"""Adversarial metadata and packaging checks, not joint runtime evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import joint_evidence as evidence
from closeout import readiness, package
spec = importlib.util.spec_from_file_location('evidence_helpers', ROOT / 'tests/test_closeout.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def xml_record():
    _, names = evidence.required_cases()
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', tests=str(len(names)), failures='0', errors='0', skipped='0')
    for name in sorted(names):
        ET.SubElement(suite, 'testcase', classname='tests.test_candidate_procurement', name=name)
    return root, suite


def test_exact_named_junit_receipt(tmp_path):
    root, _ = xml_record()
    path = tmp_path / 'report.xml'
    path.write_bytes(ET.tostring(root))
    result = evidence.check_junit(path)
    assert result['tests'] == 34 and len(result['junit_sha256']) == 64


@pytest.mark.parametrize('fault', ['missing', 'extra', 'duplicate', 'wrong_module', 'missing_class',
    'failure', 'error', 'skipped', 'summary_failure', 'summary_count', 'summary_skips',
    'missing_count', 'parent_summary', 'doctype', 'oversized', 'wrong_root', 'nested'])
def test_junit_rejects_incomplete_and_unsafe_records(tmp_path, fault):
    root, suite = xml_record()
    if fault == 'missing': suite.remove(suite[-1])
    elif fault == 'extra': ET.SubElement(suite, 'testcase', classname='tests.test_candidate_procurement', name='other')
    elif fault == 'duplicate': suite[-1].set('name', suite[0].get('name'))
    elif fault == 'wrong_module': suite[0].set('classname', 'other')
    elif fault == 'missing_class': del suite[0].attrib['classname']
    elif fault in {'failure', 'error', 'skipped'}: ET.SubElement(suite[0], fault)
    elif fault == 'summary_failure': suite.set('failures', '1')
    elif fault == 'summary_count': suite.set('tests', '340')
    elif fault == 'summary_skips': suite.set('skipped', '1')
    elif fault == 'missing_count': del suite.attrib['tests']
    elif fault == 'parent_summary': root.set('errors', '1')
    elif fault == 'wrong_root': root.tag = 'data'
    elif fault == 'nested': ET.SubElement(suite, 'testsuite')
    raw = ET.tostring(root)
    if fault == 'doctype': raw = b'<!DOCTYPE testsuites [<!ENTITY test "not allowed">]>' + raw
    if fault == 'oversized': raw = b' ' * (evidence.MAX_XML + 1)
    path = tmp_path / 'bad.xml'; path.write_bytes(raw)
    with pytest.raises((ValueError, ET.ParseError)):
        evidence.check_junit(path)


@pytest.mark.parametrize('fault', ['missing', 'wrong_tree', 'wrong_commit', 'other_repo', 'invalid_id',
    'bool_replica', 'wrong_project', 'mixed_attempt', 'mixed_run', 'duplicate_replica', 'reused_candidates'])
def test_cross_run_or_unbound_execution_cannot_pass(tmp_path, fault):
    paths = helpers.dirs(tmp_path); path = paths[1] / 'execution.json'
    r = json.loads(path.read_text())
    if fault == 'missing': path.unlink()
    elif fault == 'wrong_tree': r['source_tree'] = 'c' * 40
    elif fault == 'wrong_commit': r['source_commit'] = 'c' * 40
    elif fault == 'other_repo': r['repository'] = 'elsewhere/repo'
    elif fault == 'invalid_id': r['run_id'] = 'secret-do-not-echo'
    elif fault == 'bool_replica': r['replica'] = True
    elif fault == 'wrong_project': r['project'] = 'production'
    elif fault == 'mixed_attempt': r.update(run_attempt='2', project='pf-joint-123-2-2')
    elif fault == 'mixed_run': r.update(run_id='124', project='pf-joint-124-1-2')
    elif fault == 'duplicate_replica': r.update(replica=1, project='pf-joint-123-1-1')
    else:
        run = json.loads((paths[1] / 'roundtrip.json').read_text())
        other = json.loads((paths[0] / 'roundtrip.json').read_text())
        for a, b in zip(run['operations'], other['operations']): a['candidate_run_id'] = b['candidate_run_id']
        (paths[1] / 'roundtrip.json').write_text(json.dumps(run))
    if fault != 'missing': path.write_text(json.dumps(r))
    result = readiness('a' * 40, paths)
    assert result['status'] == 'blocked' and result['product_release_approved'] is False
    assert 'secret-do-not-echo' not in json.dumps(result)


def test_record_context_requires_ci_and_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence.subprocess, 'check_output', lambda *a, **k: 'a' * 40 + '\n' + 'b' * 40 + '\n')
    for key in ('GITHUB_ACTIONS', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'PF_JOINT_REPLICA', 'COMPOSE_PROJECT_NAME'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError): evidence.record_execution(tmp_path)
    for key, value in {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'volcano-code/ProcureFlow',
        'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','PF_JOINT_REPLICA':'2','COMPOSE_PROJECT_NAME':'pf-joint-123-1-2'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('LLM_API_KEY', 'must-never-appear')
    evidence.record_execution(tmp_path)
    assert 'must-never-appear' not in (tmp_path/'execution.json').read_text()
    with pytest.raises(FileExistsError): evidence.record_execution(tmp_path)


def test_workflow_paths_preserve_clean_source_and_read_only_permissions():
    # Check the workflow itself, without making PyYAML an application dependency.
    text = (ROOT / '.github/workflows/keyless-closeout.yml').read_text()
    assert "branches: ['develop/remediation-no-key-closeout']" in text
    assert 'max-parallel: 1' in text and 'cancel-in-progress: false' in text
    assert 'contents: read' in text and 'pull_request_target' not in text and 'secrets.' not in text
    collector = text.split('  closeout-records:', 1)[1]
    for i in (1, 2, 3):
        assert f'path: "${{{{ runner.temp }}}}/keyless-receipt{i}"' in collector
    assert 'evals/reports/ci/' not in collector
    assert 'scripts/joint_evidence.py --directory evals/reports/ci/closeout' in text


def test_artifacts_outside_checkout_allow_packaging_without_weakening_clean_guard(tmp_path):
    root = helpers.small_repo(tmp_path)
    receipts = tmp_path/'receipts';receipts.mkdir();(receipts/'report.json').write_text('{}')
    assert package(tmp_path/'good-package', root)['tracked_files'] == 2
    (root/'report.json').write_text('{}')
    with pytest.raises(ValueError, match='CLEAN_SOURCE_REQUIRED'):
        package(tmp_path/'bad-package', root)


def binding_rows():
    from fixtures.candidate_provider import VALUES
    rows=[]; versions=[];ops=[]
    for i in range(3):
        run_id,doc,quote=f'candidate_{i}',f'document_{i}',f'quote_{i}'
        values=copy.deepcopy(VALUES)
        ref={'candidate_run_id':run_id,'document_id':doc,'document_sha256':'a'*64,
            'fragment_id':'f1','origin':'model_candidate'}
        rows.append({'id':run_id,'status':'IMPORTED','document_id':doc,'quote_id':quote,
            'input_snapshot':{'document_sha256':'a'*64},'output':{'values':values}})
        versions.append({'quote_id':quote,'document_id':doc,'values':copy.deepcopy(values),
            'confirmed_by':f'buyer_{i}','evidence':{field:{**ref,'sources':[ref.copy()]} for field in VALUES}})
        ops.append({'candidate_run_id':run_id})
    rows.append({'id':'cancelled','status':'CANCELLED','quote_id':None,'output':None})
    return rows,versions,ops


def test_per_field_source_is_bound_to_its_own_imported_candidate():
    from verify_candidate_procurement import check_candidate_bindings
    rows,versions,ops=binding_rows()
    assert check_candidate_bindings(rows,versions,ops,'cancelled') is True


@pytest.mark.parametrize('fault',['empty_evidence','missing_field','other_run','cancelled_run','other_document',
    'other_source_document','other_source_hash','empty_sources','missing_fragment','wrong_origin',
    'values_changed','unconfirmed','cancelled_quote','missing_operation','duplicated_quote'])
def test_binding_rejects_empty_or_cross_candidate_provenance(fault):
    from verify_candidate_procurement import check_candidate_bindings
    rows,versions,ops=binding_rows();v=versions[0];r=v['evidence']['supplier_id']
    if fault=='empty_evidence':v['evidence']={}
    elif fault=='missing_field':v['evidence'].pop('sku')
    elif fault=='other_run':r['candidate_run_id']='candidate_1'
    elif fault=='cancelled_run':r['candidate_run_id']='cancelled'
    elif fault=='other_document':v['document_id']='document_1'
    elif fault=='other_source_document':r['sources'][0]['document_id']='document_1'
    elif fault=='other_source_hash':r['sources'][0]['document_sha256']='b'*64
    elif fault=='empty_sources':r['sources']=[]
    elif fault=='missing_fragment':r['sources'][0]['fragment_id']=''
    elif fault=='wrong_origin':r['origin']='model-claimed'
    elif fault=='values_changed':v['values']['quantity']='999'
    elif fault=='unconfirmed':v['confirmed_by']=None
    elif fault=='cancelled_quote':rows[-1]['quote_id']='quote_cancelled'
    elif fault=='missing_operation':ops.pop()
    else:versions[-1]['quote_id']=versions[0]['quote_id']
    with pytest.raises((ValueError,TypeError,KeyError)):
        check_candidate_bindings(rows,versions,ops,'cancelled')
