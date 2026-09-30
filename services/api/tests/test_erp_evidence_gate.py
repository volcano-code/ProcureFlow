"""Test report consistency only; fixtures here are not ERP execution evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module():
    spec = importlib.util.spec_from_file_location('erp_evidence_test', ROOT / 'scripts/check_erp_sandbox_evidence.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def reports(tmp_path):
    gate = module()
    values = {
        'seed.json': {'stage': 'seed', 'status': 'passed', 'synthetic_only': True,
            'integration_user_type': 'System User', 'permissions': {
                'read': True, 'create': True, 'write': True, 'submit': False, 'cancel': False, 'delete': False}},
        'roundtrip.json': {'status': 'passed', 'synthetic_only': True, 'real_user_account_used': False,
            'human_approval_measured': False, 'model_used': False, 'steps': gate.STEPS[:],
            'post_attempts': 2, 'real_erpnext_drafts_verified': 2,
            'api_business_database': 'SQLite', 'real_erp_database': 'MariaDB',
            'preflight': {'integration_identity_verified': True, 'write_probe_performed': False},
            'operations': [{'operation_id': 'synthetic-op-'+str(i), 'remote_id': 'synthetic-draft-'+str(i),
                'snapshot_hash': str(i) * 64, 'scenario': scenario, 'expected_total': '2000.00'}
                for i, scenario in enumerate(('normal', 'lost-receipt'))]},
        'database-audit.json': {'stage': 'database-audit', 'status': 'passed', 'draft_count': 2,
            'purchase_order_count': 0, 'submitted_count': 0, 'remote_unique_index_present': True,
            'duplicate_key_update_rejected_and_rolled_back': True, 'adapter_readback_independently_checked': True,
            'restricted_permissions': {'submit': True, 'cancel': True, 'delete': True},
            'erpnext_version': 'fixture', 'frappe_version': 'fixture', 'database_version': 'fixture'},
        'permission-probes.json': {'stage': 'permission-probes', 'status': 'passed',
            **{key: True for key in gate.PROBE_REQUIRED_TRUE},
            'real_user_account_used': False, 'draft_documents_checked': 2,
            'probes': [{'name': name, 'method': method, 'endpoint': endpoint, 'status': 403,
                       'error_type': 'PermissionError', 'permission_denied': True}
                      for name, method, endpoint in gate.PROBES]},
        'image-digests.json': ['frappe/erpnext@sha256:' + '0'*64],
    }
    rights = ('select', 'read', 'create', 'write', 'delete', 'submit', 'cancel',
              'amend', 'import', 'export', 'report', 'print', 'email', 'share')
    for filename in ('seed.json', 'database-audit.json'):
        values[filename]['reference_permissions'] = {right: right == 'select' for right in rights}
    def write():
        for name, value in values.items():
            (tmp_path/name).write_text(json.dumps(value))
    write()
    return gate, tmp_path, values, write


def test_valid_consistent_fixture_reports_are_hashed_not_promoted_to_new_execution(reports):
    gate, path, _, _ = reports
    result = gate.check(path)
    assert result['status'] == 'passed' and len(result['record_sha256']) == 5
    assert 'not a new ERP execution' in result['scope']


@pytest.mark.parametrize('missing', module().FILES)
def test_any_missing_stage_fails_closed(reports, missing):
    gate, path, _, _ = reports
    (path/missing).unlink()
    with pytest.raises(OSError):
        gate.check(path)


@pytest.mark.parametrize('mutation', ['failed', 'skip', 'repeated-step', 'missing-step', 'extra-write',
        'mock', 'duplicate', 'bad-hash', 'submitted', 'permission', 'index-unproven', 'user-type', 'boolean-count'])
def test_incomplete_or_unsafe_records_never_pass(reports, mutation):
    gate, path, values, write = reports
    run = values['roundtrip.json']
    audit = values['database-audit.json']
    if mutation in {'failed', 'skip'}:
        run['status'] = mutation
    elif mutation == 'repeated-step':
        run['steps'].append(run['steps'][0])
    elif mutation == 'missing-step':
        run['steps'].pop()
    elif mutation == 'extra-write':
        run['post_attempts'] = 3
    elif mutation == 'mock':
        run['operations'][0]['remote_id'] = 'MOCK-fixture'
    elif mutation == 'duplicate':
        run['operations'][1]['remote_id'] = run['operations'][0]['remote_id']
    elif mutation == 'bad-hash':
        run['operations'][0]['snapshot_hash'] = 'bad'
    elif mutation == 'submitted':
        audit['submitted_count'] = 1
    elif mutation == 'permission':
        values['seed.json']['permissions']['submit'] = True
    elif mutation == 'index-unproven':
        audit['duplicate_key_update_rejected_and_rolled_back'] = False
    elif mutation == 'boolean-count':
        audit['purchase_order_count'] = False
    elif mutation == 'user-type':
        values['seed.json']['integration_user_type'] = 'Website User'
    write()
    with pytest.raises(ValueError):
        gate.check(path)


@pytest.mark.parametrize('raw', ['{"status":"failed","status":"passed"}', 'SECRET_CANARY', 'x'*1048577])
def test_invalid_duplicate_or_oversized_json_not_echoed(reports, raw, capsys):
    gate, path, _, _ = reports
    (path/'seed.json').write_text(raw)
    assert gate.main(['--directory', str(path)]) == 1
    capture = capsys.readouterr()
    assert json.loads(capture.out) == {'status': 'failed', 'reason': 'ERP_EVIDENCE_INCOMPLETE_OR_INVALID'}
    assert 'SECRET_CANARY' not in capture.out + capture.err


@pytest.mark.parametrize('mutation', ['wrong-backend', 'no-audit', 'count', 'boolean-count',
        'wrong-version', 'not-isolated', 'missing-migration', 'unverified-probes', 'escalated-account'])
def test_combined_postgres_gate_rejects_incomplete_evidence(reports, mutation):
    gate, path, values, write = reports
    run = values['roundtrip.json']
    run['api_business_database'] = 'PostgreSQL'
    run['business_database_audit'] = {
        'status': 'passed', 'database': 'PostgreSQL', 'server_version_num': '170011',
        **{key: True for key in ('isolated_schema', 'migration_current', 'operation_identity_matches',
                                 'read_only_audit', 'after_api_restart')},
        **{key: 2 for key in ('operation_count', 'completed_operation_count', 'outbox_count',
                              'done_outbox_count', 'verified_receipt_count')}}
    write()
    result = gate.check(path, 'postgresql')
    assert result['business_database_audit_checked'] is True
    assert result['negative_rest_permissions_checked'] is True
    if mutation == 'wrong-backend':
        run['api_business_database'] = 'SQLite'
    elif mutation == 'no-audit':
        del run['business_database_audit']
    elif mutation == 'count':
        run['business_database_audit']['done_outbox_count'] = 1
    elif mutation == 'boolean-count':
        run['business_database_audit']['outbox_count'] = True
    elif mutation == 'wrong-version':
        run['business_database_audit']['server_version_num'] = 'not observed'
    elif mutation == 'not-isolated':
        run['business_database_audit']['isolated_schema'] = False
    elif mutation == 'missing-migration':
        run['business_database_audit']['migration_current'] = False
    elif mutation == 'unverified-probes':
        values['permission-probes.json']['probes'][0]['status'] = 400
    elif mutation == 'escalated-account':
        values['database-audit.json']['reference_permissions']['read'] = True
    write()
    with pytest.raises(ValueError):
        gate.check(path, 'postgresql')
