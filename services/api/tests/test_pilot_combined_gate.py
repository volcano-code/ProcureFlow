"""Offline gate consistency/mutation tests. These are not fresh actual ERP evidence."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def reports(tmp_path):
    # Shared ordinary-cell evidence fixture is independent of the acceptance checker.
    from test_erp_evidence_gate import tabular_provenance
    cases = ['csv-excluded-discount', 'xlsx-included-discount-lost-receipt']
    browser = {name: True for name in ['independent_contexts', 'distinct_users', 'native_invitation_login',
        'ordinary_table_import', 'explicit_quote_confirmation', 'independent_approval',
        'enqueue_only', 'same_origin_proxy', 'no_browser_credential_storage']}
    operations = []
    (tmp_path / 'input-fixtures').mkdir()
    for index, scenario in enumerate(cases, 1):
        lost = index == 2
        op = {'scenario': scenario, 'operation_id': str(index) * 64, 'snapshot_hash': str(index) * 64,
            'remote_id': 'synthetic-draft-' + str(index), 'final_status': 'COMPLETED',
            'expected_total': '2227.00', 'input_format': 'xlsx' if lost else 'csv',
            'browser_checks': dict(browser), 'worker_process_independent': True, 'post_attempts': 1,
            'cost_components_verified': True, 'tabular_provenance_verified': True, 'readback_verified': True,
            'logout_before_recovery': lost, 'recovery_read_only': lost, 'api_restarted_before_recovery': lost,
            'session_expired_after_enqueue': not lost, 'stale_session_denied': True, 'stale_session_mutation_denied': True,
            'accepted_work_survives_session_expiry': not lost}
        op['provenance'] = tabular_provenance(op, index)
        content = ('offline fixture only: ' + scenario).encode()
        filename = f"synthetic-{scenario}.{op['input_format']}"
        (tmp_path / 'input-fixtures' / filename).write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        op['provenance']['document_sha256'] = sha
        for field in op['provenance']['field_evidence'].values(): field['document_sha256'] = sha
        operations.append(op)
    denials = [{'scenario': case, 'operation_id': str(index) * 64, 'snapshot_hash': str(index) * 64,
        'remote_id': None, 'final_status': 'NEEDS_HUMAN', 'stale_session_http_status': 401,
        'denial_reason': 'BUYER_REVOKED' if case == 'buyer-revocation' else 'APPROVER_REVOKED',
        'api_restarted': True, 'fresh_workers': 2, 'first_write_attempts': 0, 'browser_checks': dict(browser)}
        for index, case in enumerate(['buyer-revocation', 'approver-revocation', 'approver-membership-expiry'], 3)]
    rights = {right: right == 'select' for right in ['select', 'read', 'create', 'write', 'delete',
        'submit', 'cancel', 'amend', 'import', 'export', 'report', 'print', 'email', 'share']}
    values = {
        'seed.json': {'status': 'passed', 'stage': 'seed', 'synthetic_only': True,
            'reference_permissions': dict(rights), 'cost_reference_permissions': {'tax': dict(rights), 'freight': dict(rights)}},
        'roundtrip.json': {'status': 'passed', 'synthetic_only': True, 'fixture': False, 'live_erp': True,
            'model_used': False, 'human_approval_measured': False, 'real_user_account_used': False,
            'browser_verified': True, 'api_business_database': 'sqlite', 'real_erp_database': 'MariaDB',
            'steps': ['production_next_build', 'dedicated_identity_get_only_preflight',
                'native_csv_browser_to_worker_draft', 'native_xlsx_lost_receipt_read_only_after_logout',
                'buyer_revocation_denies_restarted_worker_first_write', 'approver_revocation_denies_restarted_worker_first_write',
                'approver_membership_expiry_denies_restarted_worker_first_write', 'independent_business_database_audit'],
            'post_attempts': 2, 'real_erpnext_drafts_verified': 2, 'mock_drafts_verified': 0,
            'preflight': {'integration_identity_verified': True, 'company_currency_verified': True, 'write_probe_performed': False},
            'operations': operations, 'denials': denials,
            'business_database_audit': {'status': 'passed', 'database': 'sqlite', 'read_only_audit': True,
                'after_api_restart': True, 'migration_current': True, 'operation_identity_matches': True,
                'durable_pilot_identity_rows': True, 'operation_count': 5, 'completed_operation_count': 2,
                'denied_operation_count': 3, 'done_outbox_count': 5, 'verified_receipt_count': 2,
                'denied_dispatch_attempts': 0, 'buyer_approver_binding_checked': True}},
        'database-audit.json': {'status': 'passed', 'stage': 'database-audit', 'scope': 'pilot-native-two-draft',
            'draft_count': 2, 'purchase_order_count': 0, 'submitted_count': 0, 'denied_operation_draft_count': 0,
            'operation_count_checked': 5, 'read_only_audit': True, 'remote_unique_index_present': True,
            'adapter_readback_independently_checked': True, 'cost_components_independently_checked': True,
            'purchase_order_create_denied': True, 'restricted_permissions': {'submit': True, 'cancel': True, 'delete': True},
            'reference_permissions': dict(rights), 'cost_reference_permissions': {'tax': dict(rights), 'freight': dict(rights)},
            'erpnext_version': 'fixture', 'frappe_version': 'fixture', 'database_version': 'fixture'},
        'image-digests.json': ['frappe/erpnext@sha256:' + 'a' * 64],
    }
    def save():
        for name, value in values.items(): (tmp_path / name).write_text(json.dumps(value))
    save()
    return tmp_path, values, save


def test_gate_complete_fixture_is_only_record_consistency(reports):
    directory, _, _ = reports
    result = module('check_pilot_combined_evidence').check(directory)
    assert result['status'] == 'passed'
    assert 'not a new ERP execution' in result['scope']


@pytest.mark.parametrize('mapping_order', ['api', 'browser', 'reversed-api', 'alphabetical'])
@pytest.mark.parametrize('json_order', ['original', 'reversed', 'sorted'])
def test_pilot_gate_accepts_mapping_order_without_json_order_dependency(reports, mapping_order, json_order):
    from test_erp_evidence_gate import MAPPING_ORDERS, reverse_json_objects, set_fragment_order
    directory, values, save = reports
    for operation in values['roundtrip.json']['operations']:
        set_fragment_order(operation['provenance'], MAPPING_ORDERS[mapping_order])
    save()
    for name, value in values.items():
        if json_order == 'reversed':
            value = reverse_json_objects(value)
        (directory / name).write_text(json.dumps(value, sort_keys=json_order == 'sorted'))
    assert module('check_pilot_combined_evidence').check(directory)['status'] == 'passed'


@pytest.mark.parametrize('operation_index', [0, 1])
@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'wrong-document', 'out-of-range'])
def test_pilot_gate_rejects_invalid_fragment_sets(reports, operation_index, mutation):
    from test_erp_evidence_gate import MAPPING_ORDERS, set_fragment_order
    directory, values, save = reports
    provenance = values['roundtrip.json']['operations'][operation_index]['provenance']
    set_fragment_order(provenance, MAPPING_ORDERS['browser'])
    evidence = provenance['field_evidence']['unit_price']
    if mutation == 'missing':
        del evidence['fragment_id']
    elif mutation == 'duplicate':
        evidence['fragment_id'] = provenance['field_evidence']['supplier_id']['fragment_id']
    elif mutation == 'wrong-document':
        evidence['fragment_id'] = 'doc_' + 'f' * 32 + ':f0004'
    else:
        evidence['fragment_id'] = provenance['document_id'] + ':f0011'
    save()
    with pytest.raises(ValueError, match='INVALID_TABULAR_FIELD_EVIDENCE'):
        module('check_pilot_combined_evidence').check(directory)


@pytest.mark.parametrize('path,value', [
    ('roundtrip.json/fixture', True), ('roundtrip.json/model_used', True),
    ('roundtrip.json/live_erp', False), ('roundtrip.json/browser_verified', False),
    ('roundtrip.json/post_attempts', True), ('roundtrip.json/post_attempts', 3),
    ('roundtrip.json/real_erpnext_drafts_verified', 8), ('roundtrip.json/mock_drafts_verified', 1),
    ('roundtrip.json/operations/0/worker_process_independent', False),
    ('roundtrip.json/operations/0/browser_checks/distinct_users', False),
    ('roundtrip.json/operations/0/browser_checks/native_invitation_login', False),
    ('roundtrip.json/operations/0/browser_checks/ordinary_table_import', False),
    ('roundtrip.json/operations/0/browser_checks/enqueue_only', False),
    ('roundtrip.json/operations/0/browser_checks/same_origin_proxy', False),
    ('roundtrip.json/operations/0/remote_id', 'MOCK-draft'),
    ('roundtrip.json/operations/0/post_attempts', 2),
    ('roundtrip.json/operations/0/accepted_work_survives_session_expiry', False),
    ('roundtrip.json/operations/1/logout_before_recovery', False),
    ('roundtrip.json/operations/1/recovery_read_only', False),
    ('roundtrip.json/operations/1/api_restarted_before_recovery', False),
    ('roundtrip.json/denials/0/first_write_attempts', 1),
    ('roundtrip.json/denials/1/fresh_workers', True),
    ('roundtrip.json/denials/2/stale_session_http_status', 403),
    ('roundtrip.json/denials/2/final_status', 'COMPLETED'),
    ('roundtrip.json/denials/2/denial_reason', 'EXECUTION_TARGET_OR_SNAPSHOT_CHANGED'),
    ('roundtrip.json/operations/0/stale_session_mutation_denied', False),
    ('roundtrip.json/business_database_audit/read_only_audit', False),
    ('roundtrip.json/business_database_audit/done_outbox_count', 2),
    ('roundtrip.json/business_database_audit/denied_dispatch_attempts', 1),
    ('roundtrip.json/business_database_audit/verified_receipt_count', 1),
    ('roundtrip.json/business_database_audit/buyer_approver_binding_checked', False),
    ('database-audit.json/draft_count', 3), ('database-audit.json/purchase_order_count', 1),
    ('database-audit.json/submitted_count', 1), ('database-audit.json/denied_operation_draft_count', 1),
    ('database-audit.json/remote_unique_index_present', False),
    ('database-audit.json/reference_permissions/read', True),
    ('database-audit.json/cost_reference_permissions/tax/write', True),
])
def test_gate_rejects_incomplete_or_unsafe_evidence(reports, path, value):
    directory, values, save = reports
    segments = path.split('/')
    current = values
    for segment in segments[:-1]: current = current[int(segment) if isinstance(current, list) else segment]
    current[segments[-1]] = value
    save()
    with pytest.raises(ValueError): module('check_pilot_combined_evidence').check(directory)


def test_gate_rejects_missing_reordered_and_duplicate_operations(reports):
    directory, values, save = reports
    original = deepcopy(values['roundtrip.json']['operations'])
    gate = module('check_pilot_combined_evidence')
    for operations in [original[:1], original[::-1], [original[0], original[0]]]:
        values['roundtrip.json']['operations'] = operations; save()
        with pytest.raises(ValueError): gate.check(directory)


def test_gate_requires_independent_postgres_evidence(reports):
    directory, values, save = reports
    gate = module('check_pilot_combined_evidence')
    with pytest.raises(ValueError): gate.check(directory, 'postgresql')
    run = values['roundtrip.json']; run['api_business_database'] = 'postgresql'
    run['business_database_audit'].update(database='postgresql', isolated_schema=True, server_version_num='170005')
    save()
    assert gate.check(directory, 'postgresql')['status'] == 'passed'
    run['business_database_audit']['isolated_schema'] = False; save()
    with pytest.raises(ValueError): gate.check(directory, 'postgresql')


def test_gate_rejects_tampered_source_and_duplicate_json(reports):
    directory, _, _ = reports
    file = next((directory / 'input-fixtures').iterdir()); file.write_bytes(b'changed')
    gate = module('check_pilot_combined_evidence')
    with pytest.raises(ValueError): gate.check(directory)
    (directory / 'roundtrip.json').write_text('{"status":"passed","status":"failed"}')
    with pytest.raises(ValueError): gate.check(directory)


@pytest.mark.parametrize('flags', [[], ['--ephemeral-test']])
def test_runner_requires_explicit_valid_lab_before_network(tmp_path, monkeypatch, flags):
    runner = module('verify_pilot_combined')
    monkeypatch.setattr(runner, 'exercise', lambda *_: pytest.fail('unauthorized network'))
    out = tmp_path / 'report.json'
    assert runner.main(flags + ['--credentials', str(tmp_path / 'missing'), '--env-file', str(tmp_path / 'missing-env'),
        '--output', str(out)]) == 2
    assert json.loads(out.read_text())['network_attempted'] is False
    assert not (tmp_path / 'input-fixtures').exists()


def test_runner_strips_ambient_secrets_and_endpoints(monkeypatch):
    runner = module('verify_pilot_combined')
    for key in ['PF_AUTH_TOKENS', 'PF_DATABASE_URL', 'ERP_API_SECRET', 'ERP_BASE_URL', 'LLM_API_KEY',
                'NEXT_PUBLIC_SECRET', 'PF_INTERNAL_API_ORIGIN', 'PGHOST', 'OPENAI_API_KEY']:
        monkeypatch.setenv(key, 'do-not-leak-ambient')
    env = runner.isolated_environment('/tmp/test', 'sqlite:////tmp/test/app.sqlite3',
        'http://127.0.0.1:1234', 'http://127.0.0.1:2345', {}, '', True)
    assert 'do-not-leak-ambient' not in json.dumps(env)
    assert env['PF_MODE'] == 'pilot' and env['PF_ERP_MODE'] == 'mock'
    assert env['ERP_ALLOW_DRAFT_WRITES'] == 'false'
    assert env['NEXT_PUBLIC_API_BASE_URL'] == '/backend'


def test_dedicated_ci_preserves_existing_eight_case_erp_contract():
    workflow = (ROOT / '.github/workflows/pilot-erp.yml').read_text()
    assert 'business-database: [sqlite, postgresql]' in workflow
    assert 'verify_pilot_combined.py --ephemeral-test' in workflow
    assert 'check_pilot_combined_evidence.py --business-database' in workflow
    assert 'pilot_audit.py' in workflow and 'down -v --remove-orphans' in workflow
    assert 'continue-on-error' not in workflow and '--fixture' not in workflow
    existing = module('check_erp_sandbox_evidence')
    assert len(existing.ACCEPTANCE_CASES) == 8


@pytest.fixture
def business_audit_fixture(tmp_path, reports):
    """Minimal independent SQL rows test the audit; not browser/ERP acceptance."""
    from alembic.script import ScriptDirectory
    import sqlite3
    _, values, _ = reports
    run = values['roundtrip.json']
    file = tmp_path / 'business.sqlite3'
    with sqlite3.connect(file) as db:
        db.executescript('''
            CREATE TABLE alembic_version(version_num TEXT);
            CREATE TABLE external_operations(id TEXT, tenant_id TEXT, status TEXT, snapshot_hash TEXT,
                remote_id TEXT, error TEXT, attempts INTEGER, approval_id TEXT, initiator_id TEXT, initiator_auth_version TEXT);
            CREATE TABLE approvals(id TEXT, approver_id TEXT, approver_auth_version TEXT);
            CREATE TABLE outbox(operation_id TEXT, status TEXT);
            CREATE TABLE audit_events(type TEXT, payload TEXT);
            CREATE TABLE pilot_sessions(id TEXT);
            CREATE TABLE pilot_memberships(id TEXT);
        ''')
        for head in ScriptDirectory(str(ROOT / 'services/api/alembic')).get_heads():
            db.execute('INSERT INTO alembic_version VALUES (?)', (head,))
        for index, op in enumerate(run['operations'] + run['denials']):
            db.execute('INSERT INTO external_operations VALUES (?,?,?,?,?,?,?,?,?,?)',
                (op['operation_id'], 'lab', op['final_status'], op['snapshot_hash'], op['remote_id'],
                 op.get('denial_reason'), 1 if index < 2 else 0, f'a{index}', f'buyer{index}', '1:1'))
            db.execute('INSERT INTO approvals VALUES (?,?,?)', (f'a{index}', f'approver{index}', '1:1'))
            db.execute('INSERT INTO outbox VALUES (?,?)', (op['operation_id'], 'DONE'))
            if index < 2:
                receipt = {key: op[key] for key in ('operation_id', 'snapshot_hash', 'remote_id')}
                receipt.update(simulated=False, external_write_attempted=False)
                db.execute('INSERT INTO audit_events VALUES (?,?)', ('ERP_VERIFICATION_VERIFIED', json.dumps(receipt)))
        for index in range(10):
            db.execute('INSERT INTO pilot_sessions VALUES (?)', (str(index),))
            db.execute('INSERT INTO pilot_memberships VALUES (?)', (str(index),))
    return file, run


def test_independent_sql_audit_only_selects(business_audit_fixture, monkeypatch):
    from sqlalchemy import create_engine, event
    file, run = business_audit_fixture
    runner = module('verify_pilot_combined')
    engine = create_engine(f'sqlite:///{file}')
    statements = []
    event.listen(engine, 'before_cursor_execute', lambda _c, _cur, statement, _p, _ctx, _many: statements.append(statement))
    monkeypatch.setattr(runner, 'create_engine', lambda *_a, **_kw: engine)
    result = runner.audit_business(f'sqlite:///{file}', run['operations'], run['denials'], 'sqlite')
    assert result['buyer_approver_binding_checked'] and result['verified_receipt_count'] == 2
    assert statements and all(statement.lstrip().upper().startswith('SELECT ') for statement in statements)


@pytest.mark.parametrize('mutation', [
    "UPDATE external_operations SET initiator_id='approver0' WHERE approval_id='a0'",
    "UPDATE external_operations SET initiator_auth_version=NULL WHERE approval_id='a0'",
    "UPDATE external_operations SET attempts=1 WHERE approval_id='a3'",
    "UPDATE external_operations SET error='WRONG_DENIAL' WHERE approval_id='a3'",
    "DELETE FROM audit_events",
    "UPDATE audit_events SET payload=replace(payload, 'false', 'true')",
    "UPDATE outbox SET status='PENDING'",
    "DELETE FROM pilot_sessions",
])
def test_independent_sql_audit_rejects_missing_binding_or_side_effects(business_audit_fixture, mutation):
    import sqlite3
    file, run = business_audit_fixture
    with sqlite3.connect(file) as db: db.execute(mutation)
    with pytest.raises(AssertionError):
        module('verify_pilot_combined').audit_business(f'sqlite:///{file}', run['operations'], run['denials'], 'sqlite')
