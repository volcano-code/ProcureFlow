"""Test report consistency only; fixtures here are not ERP execution evidence."""
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module():
    spec = importlib.util.spec_from_file_location('erp_evidence_test', ROOT / 'scripts/check_erp_sandbox_evidence.py')
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


# Independently spelled-out evidence contract; do not generate from the checker.
MAPPING = {
    'currency': 'B', 'shipping_cost': 'C', 'supplier_id': 'D', 'tax_mode': 'E',
    'sku': 'F', 'discount': 'G', 'unit_price': 'H', 'delivery_days': 'I',
    'tax_rate': 'J', 'quantity': 'K', 'uom': 'L',
}

HEADERS = ['币种', '运费', '供应商编码', '税价模式', '物料编码', '折扣金额',
           '单价', '交期天数', '税率', '数量', '单位']


def tabular_provenance(operation, index):
    kind = operation['input_format']
    included = 'included-discount' in operation['scenario']
    suffix = f'{index:032x}'
    document_id, sha = 'doc_' + suffix, f'{index:064x}'
    sheet = 'CSV' if kind == 'csv' else '报价明细'
    values = {'supplier_id': 'Synthetic Supplier', 'sku': 'PF-SANDBOX-ITEM',
        'quantity': '20', 'uom': 'EA', 'currency': 'CNY', 'delivery_days': 7,
        'unit_price': '113.00' if included else '100.00',
        'tax_mode': 'included' if included else 'excluded', 'tax_rate': '0.13',
        'shipping_cost': '80.00', 'discount': '113.00' if included else '100.00'}
    raw = {**values, 'tax_mode': '含税' if included else '未税', 'tax_rate': '13%', 'currency': '人民币'}
    fields = {}
    for number, (field, column) in enumerate(MAPPING.items()):
        fields[field] = {'kind': kind, 'document_id': document_id, 'document_sha256': sha,
            'fragment_id': f'{document_id}:f{number:04d}', 'text': f'{HEADERS[number]}: {raw[field]}',
            'sheet': sheet, 'row': 5, 'column': column, 'cell_range': f'{column}5',
            'header_row': 3, 'label_cell': f'{column}3'}
        if kind == 'csv':
            fields[field].update(line_start=6, line_end=6)
    return {'document_sha256': sha, 'document_id': document_id, 'import_id': 'tim_' + suffix,
        'quote_id': 'quo_' + suffix, 'selected_sheet': sheet, 'header_row': 3, 'selected_row': 5,
        'column_mapping': dict(MAPPING), 'quote_values': values, 'field_evidence': fields}


@pytest.fixture
def reports(tmp_path):
    gate = module()
    values = {
        'seed.json': {'stage': 'seed', 'status': 'passed', 'synthetic_only': True,
            'integration_user_type': 'System User', 'permissions': {
                'read': True, 'create': True, 'write': True, 'submit': False, 'cancel': False, 'delete': False}},
        'roundtrip.json': {'status': 'passed', 'synthetic_only': True, 'real_user_account_used': False,
            'human_approval_measured': False, 'model_used': False, 'steps': gate.STEPS[:],
            'post_attempts': 8, 'real_erpnext_drafts_verified': 8,
            'api_business_database': 'SQLite', 'real_erp_database': 'MariaDB',
            'preflight': {'integration_identity_verified': True, 'company_currency_verified': True, 'write_probe_performed': False},
            'operations': [{'operation_id': 'synthetic-op-'+str(i), 'remote_id': 'synthetic-draft-'+str(i),
                'snapshot_hash': str(i) * 64, 'scenario': scenario, 'expected_total': gate.ACCEPTANCE_CASES[scenario]['total'], 'cost_components_verified': True,
                'input_format': gate.ACCEPTANCE_CASES[scenario]['input_format']}
                for i, scenario in enumerate(gate.ACCEPTANCE_CASES)]},
        'database-audit.json': {'stage': 'database-audit', 'status': 'passed', 'draft_count': 8,
            'purchase_order_count': 0, 'submitted_count': 0, 'remote_unique_index_present': True,
            'duplicate_key_update_rejected_and_rolled_back': True, 'adapter_readback_independently_checked': True, 'cost_components_independently_checked': True,
            'restricted_permissions': {'submit': True, 'cancel': True, 'delete': True},
            'erpnext_version': 'fixture', 'frappe_version': 'fixture', 'database_version': 'fixture'},
        'permission-probes.json': {'stage': 'permission-probes', 'status': 'passed',
            **{key: True for key in gate.PROBE_REQUIRED_TRUE},
            'real_user_account_used': False, 'draft_documents_checked': 8,
            'probes': [{'name': name, 'method': method, 'endpoint': endpoint, 'status': 403,
                       'error_type': 'PermissionError', 'permission_denied': True}
                      for name, method, endpoint in gate.PROBES]},
        'image-digests.json': ['frappe/erpnext@sha256:' + '0'*64],
    }
    (tmp_path / 'input-fixtures').mkdir()
    for index, operation in enumerate(values['roundtrip.json']['operations']):
        if operation['input_format'] in {'csv', 'xlsx'}:
            operation.update(tabular_provenance_verified=True, duplicate_import_reused=True,
                preview_import_restart_verified=True, provenance=tabular_provenance(operation, index))
            # Simple independent bytes exercise hash consistency only. Real CSV
            # and XLSX parsing is covered by separate acceptance harness tests.
            content = f"offline gate fixture only: {operation['scenario']}\n".encode()
            filename = f"synthetic-{operation['scenario']}.{operation['input_format']}"
            (tmp_path / 'input-fixtures' / filename).write_bytes(content)
            sha = hashlib.sha256(content).hexdigest()
            operation['provenance']['document_sha256'] = sha
            for evidence in operation['provenance']['field_evidence'].values():
                evidence['document_sha256'] = sha
    rights = ('select', 'read', 'create', 'write', 'delete', 'submit', 'cancel',
              'amend', 'import', 'export', 'report', 'print', 'email', 'share')
    for filename in ('seed.json', 'database-audit.json'):
        values[filename].update(currency_precision='2', float_precision='6', rounding_method='Commercial Rounding')
        values[filename]['reference_permissions'] = {right: right == 'select' for right in rights}
        values[filename]['cost_reference_permissions'] = {kind: {right: right == 'select' for right in rights} for kind in ('tax', 'freight')}
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
    assert len(result['source_sha256']) == 4
    assert result['source_sha256'] == {str(source.relative_to(path)): hashlib.sha256(source.read_bytes()).hexdigest()
        for source in (path / 'input-fixtures').iterdir()}


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
        **{key: 8 for key in ('operation_count', 'completed_operation_count', 'outbox_count',
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


@pytest.mark.parametrize('change', ['missing-cost-proof', 'wrong-total', 'old-count', 'precision',
    'rounding', 'float-precision', 'missing-account', 'escalated-account', 'missing-db-proof'])
def test_cost_increment_requires_all_eight_precise_independent_results(reports, change):
    gate, path, values, write = reports
    if change == 'missing-cost-proof': del values['roundtrip.json']['operations'][0]['cost_components_verified']
    elif change == 'wrong-total': values['roundtrip.json']['operations'][0]['expected_total'] = '24800.01'
    elif change == 'old-count': values['roundtrip.json']['operations'] = values['roundtrip.json']['operations'][:2]
    elif change == 'precision': values['database-audit.json']['currency_precision'] = '3'
    elif change == 'rounding': values['database-audit.json']['rounding_method'] = "Banker's Rounding"
    elif change == 'float-precision': del values['seed.json']['float_precision']
    elif change == 'missing-account': del values['seed.json']['cost_reference_permissions']['tax']
    elif change == 'escalated-account': values['database-audit.json']['cost_reference_permissions']['freight']['read'] = True
    elif change == 'missing-db-proof': del values['database-audit.json']['cost_components_independently_checked']
    write()
    with pytest.raises(ValueError): gate.check(path)


def test_company_currency_preflight_evidence_is_required(reports):
    gate, path, values, write=reports
    values['roundtrip.json']['preflight'].pop('company_currency_verified')
    write()
    with pytest.raises(ValueError): gate.check(path)


@pytest.mark.parametrize('scenario_index', [4, 5, 6, 7])
@pytest.mark.parametrize('claim', ['tabular_provenance_verified', 'duplicate_import_reused', 'preview_import_restart_verified'])
@pytest.mark.parametrize('bad_value', [False, 1, 'true', None])
def test_each_tabular_case_requires_strict_positive_claims(reports, scenario_index, claim, bad_value):
    gate, path, values, write = reports
    operation = values['roundtrip.json']['operations'][scenario_index]
    operation[claim] = bad_value
    write()
    with pytest.raises(ValueError, match='TABULAR_CHECKS_MISSING'):
        gate.check(path)


@pytest.mark.parametrize('scenario_index', range(8))
@pytest.mark.parametrize('bad_format', [None, 'pdf', 'CSV'])
def test_all_cases_require_matching_input_format(reports, scenario_index, bad_format):
    gate, path, values, write = reports
    values['roundtrip.json']['operations'][scenario_index]['input_format'] = bad_format
    write()
    with pytest.raises(ValueError, match='INVALID_INPUT_FORMAT'):
        gate.check(path)


@pytest.mark.parametrize('mutation', ['original-four-only', 'reorder', 'duplicate-scenario',
    'wrong-post-count', 'wrong-draft-count', 'wrong-audit-count', 'wrong-probe-count', 'missing-tabular-step'])
def test_acceptance_requires_all_eight_ordered_scenarios_and_counts(reports, mutation):
    gate, path, values, write = reports
    run = values['roundtrip.json']
    if mutation == 'original-four-only': run['operations'] = run['operations'][:4]
    elif mutation == 'reorder': run['operations'][4:6] = reversed(run['operations'][4:6])
    elif mutation == 'duplicate-scenario': run['operations'][7]['scenario'] = run['operations'][6]['scenario']
    elif mutation == 'wrong-post-count': run['post_attempts'] = 4
    elif mutation == 'wrong-draft-count': run['real_erpnext_drafts_verified'] = 4
    elif mutation == 'wrong-audit-count': values['database-audit.json']['draft_count'] = 4
    elif mutation == 'wrong-probe-count': values['permission-probes.json']['draft_documents_checked'] = 4
    elif mutation == 'missing-tabular-step': run['steps'].remove('tabular_preview_import_and_stale_quote_policy_writes_denied')
    write()
    with pytest.raises(ValueError): gate.check(path)


@pytest.mark.parametrize('scenario_index', [4, 5, 6, 7])
@pytest.mark.parametrize('mutation', ['missing', 'bad-sha', 'bad-import-id', 'bad-quote-id', 'bad-document-id',
    'sheet', 'header', 'boolean-header', 'row', 'duplicate-row', 'mapping', 'extra-mapping',
    'missing-value', 'value', 'price-float', 'boolean-days', 'supplier', 'missing-field', 'extra-field',
    'field-hash', 'field-document', 'field-fragment', 'field-kind', 'field-sheet', 'field-row',
    'field-column', 'field-cell', 'field-header', 'field-label', 'field-lines', 'field-text', 'field-extra'])
def test_tabular_provenance_fails_closed_on_missing_or_drifted_claims(reports, scenario_index, mutation):
    gate, path, values, write = reports
    operation = values['roundtrip.json']['operations'][scenario_index]
    provenance = operation['provenance']
    evidence = provenance['field_evidence']['unit_price']
    if mutation == 'missing': del operation['provenance']
    elif mutation == 'bad-sha': provenance['document_sha256'] = 'missing'
    elif mutation == 'bad-import-id': provenance['import_id'] = 'tim_'
    elif mutation == 'bad-quote-id': provenance['quote_id'] = 'tim_' + 'a' * 32
    elif mutation == 'bad-document-id': provenance['document_id'] = 'doc_' + 'f' * 32
    elif mutation == 'sheet': provenance['selected_sheet'] = 'Other'
    elif mutation == 'header': provenance['header_row'] = 2
    elif mutation == 'boolean-header': provenance['header_row'] = True
    elif mutation == 'row': provenance['selected_row'] = 4
    elif mutation == 'duplicate-row': provenance['selected_row'] = 6
    elif mutation == 'mapping': provenance['column_mapping']['unit_price'] = 'G'
    elif mutation == 'extra-mapping': provenance['column_mapping']['note'] = 'A'
    elif mutation == 'missing-value': del provenance['quote_values']['discount']
    elif mutation == 'value': provenance['quote_values']['discount'] = '0.00'
    elif mutation == 'price-float': provenance['quote_values']['unit_price'] = 100.0
    elif mutation == 'boolean-days': provenance['quote_values']['delivery_days'] = True
    elif mutation == 'supplier': provenance['quote_values']['supplier_id'] = ''
    elif mutation == 'missing-field': del provenance['field_evidence']['discount']
    elif mutation == 'extra-field': provenance['field_evidence']['note'] = dict(evidence)
    elif mutation == 'field-hash': evidence['document_sha256'] = 'f' * 64
    elif mutation == 'field-document': evidence['document_id'] = 'doc_' + 'f' * 32
    elif mutation == 'field-fragment': evidence['fragment_id'] = 'unrelated:f0000'
    elif mutation == 'field-kind': evidence['kind'] = 'source'
    elif mutation == 'field-sheet': evidence['sheet'] = 'Other'
    elif mutation == 'field-row': evidence['row'] = 6
    elif mutation == 'field-column': evidence['column'] = 'G'
    elif mutation == 'field-cell': evidence['cell_range'] = 'H6'
    elif mutation == 'field-header': evidence['header_row'] = 2
    elif mutation == 'field-label': evidence['label_cell'] = 'H2'
    elif mutation == 'field-lines': evidence.update(line_start=5, line_end=5)
    elif mutation == 'field-text': evidence['text'] = 'unit_price: 0.00'
    elif mutation == 'field-extra': evidence['unrecognized'] = 'claim'
    write()
    with pytest.raises(ValueError): gate.check(path)


@pytest.mark.parametrize('key', ['document_sha256', 'import_id', 'quote_id'])
def test_distinct_tabular_acceptance_cases_require_distinct_provenance(reports, key):
    gate, path, values, write = reports
    operations = values['roundtrip.json']['operations']
    source, target = operations[4]['provenance'], operations[6]['provenance']
    target[key] = source[key]
    if key == 'import_id':
        target['document_id'] = source['document_id']
    for evidence in target['field_evidence'].values():
        evidence['document_sha256'] = target['document_sha256']
        evidence['document_id'] = target['document_id']
        evidence['fragment_id'] = target['document_id'] + ':' + evidence['fragment_id'].split(':')[1]
    write()
    with pytest.raises(ValueError, match='DUPLICATE_TABULAR_PROVENANCE'):
        gate.check(path)


@pytest.mark.parametrize('scenario', ['csv-excluded-discount', 'csv-included-discount-lost-receipt',
    'xlsx-excluded-discount', 'xlsx-included-discount-lost-receipt'])
@pytest.mark.parametrize('mutation', ['missing', 'tampered', 'oversized', 'empty'])
def test_retained_original_sources_are_required_bounded_and_hash_bound(reports, scenario, mutation):
    gate, path, _, _ = reports
    kind = scenario.split('-')[0]
    source = path / 'input-fixtures' / f'synthetic-{scenario}.{kind}'
    if mutation == 'missing': source.unlink()
    elif mutation == 'tampered': source.write_bytes(source.read_bytes() + b'changed')
    elif mutation == 'oversized': source.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    elif mutation == 'empty': source.write_bytes(b'')
    with pytest.raises((OSError, ValueError)):
        gate.check(path)


def test_retained_sources_cannot_be_swapped_between_same_format_scenarios(reports):
    gate, path, _, _ = reports
    excluded = path / 'input-fixtures/synthetic-csv-excluded-discount.csv'
    included = path / 'input-fixtures/synthetic-csv-included-discount-lost-receipt.csv'
    excluded_bytes, included_bytes = excluded.read_bytes(), included.read_bytes()
    excluded.write_bytes(included_bytes)
    included.write_bytes(excluded_bytes)
    with pytest.raises(ValueError, match='SOURCE_FIXTURE_HASH_MISMATCH'):
        gate.check(path)


def test_source_limit_allows_exactly_two_mib_and_reports_independent_digest(reports):
    gate, path, values, write = reports
    source = path / 'input-fixtures/synthetic-csv-excluded-discount.csv'
    content = b'x' * (2 * 1024 * 1024)
    source.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    provenance = values['roundtrip.json']['operations'][4]['provenance']
    provenance['document_sha256'] = sha
    for evidence in provenance['field_evidence'].values():
        evidence['document_sha256'] = sha
    write()
    result = gate.check(path)
    assert result['source_sha256']['input-fixtures/synthetic-csv-excluded-discount.csv'] == sha
    assert len(result['record_sha256']) == 5


def test_source_failure_cli_never_echoes_source_bytes_or_path(reports, capsys):
    gate, path, _, _ = reports
    source = path / 'input-fixtures/synthetic-csv-excluded-discount.csv'
    source.write_bytes(b'SECRET_SOURCE_CANARY')
    assert gate.main(['--directory', str(path)]) == 1
    capture = capsys.readouterr()
    assert json.loads(capture.out) == {'status': 'failed', 'reason': 'ERP_EVIDENCE_INCOMPLETE_OR_INVALID'}
    assert 'SECRET_SOURCE_CANARY' not in capture.out + capture.err
    assert str(path) not in capture.out + capture.err
