"""Executable multi-item CI selection and offline source/evidence checks, never live evidence."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'integrations/erpnext/sandbox'))
from cost_fixtures import MULTI_ITEM_CASE, MULTI_ITEM_SCENARIO, acceptance_cases
import erp_multi_item_fixtures as fixtures
from test_erp_evidence_gate import reports
from procureflow.tabular import parse_table, map_table_rows

DATA = {'supplier': 'Synthetic Supplier', 'sku': 'PF-SANDBOX-ITEM'}


def provenance():
    doc = 'doc_' + 'a' * 32
    sha = hashlib.sha256(fixtures.source_file(DATA)[1]).hexdigest()
    return {'document_sha256': sha, 'document_id': doc, 'import_id': 'tim_' + 'a' * 32,
        'quote_id': 'quo_' + 'a' * 32, 'selected_sheet': 'CSV', 'header_row': 1,
        'selected_rows': [2, 3], 'column_mapping': dict(fixtures.MAPPING),
        'quote_values': fixtures.expected_values(DATA), 'field_evidence': fixtures.expected_evidence(DATA, doc, sha)}


def expand_reports(reports):
    gate, path, values, write = reports
    run, audit, seed = (values[name] for name in ('roundtrip.json', 'database-audit.json', 'seed.json'))
    run.update(include_multi_item=True, post_attempts=9, real_erpnext_drafts_verified=9)
    run['steps'].insert(-1, MULTI_ITEM_SCENARIO + '_independent_worker_draft_readback_and_replay')
    run['operations'].append({'operation_id': 'synthetic-multi-op', 'remote_id': 'synthetic-multi-draft',
        'snapshot_hash': 'a' * 64, 'scenario': MULTI_ITEM_SCENARIO, 'expected_total': '47.05',
        'cost_components_verified': True, 'input_format': 'csv', 'multi_item': True, 'item_count': 2,
        'shipping_count': 1, 'supplier_id': DATA['supplier'], 'transaction_date': '2026-10-08',
        'contract_version': 'multi-sku-v1', 'erp_cost_mapping_version': 'multi-line-zero-tax-costs-v1', **{key: True for key in ('multi_item_provenance_verified', 'duplicate_import_reused',
        'preview_import_restart_verified', 'independent_readback_verified', 'lost_receipt_reconciled',
        'idempotent_replay_verified', 'single_post_verified', 'source_rows_verified')}, 'provenance': provenance()})
    audit.update(draft_count=9, multi_item_components_independently_checked=True,
                 multi_item_draft_count=1, multi_item_row_count=2)
    seed.update(synthetic_item_count=2, multi_skus=['PF-SANDBOX-ITEM', 'PF-SANDBOX-ITEM-2'])
    values['permission-probes.json']['draft_documents_checked'] = 9
    fixtures.save_source_file(path, DATA)
    write()
    return gate, path, values, write


def test_two_row_source_parses_to_complete_independent_provenance():
    filename, content = fixtures.source_file(DATA)
    p = provenance()
    mapped = map_table_rows(parse_table(filename, content), 'CSV', 1, [2, 3],
        fixtures.MAPPING, p['document_id'], p['document_sha256'])
    assert mapped['values'] == p['quote_values']
    assert mapped['evidence'] == p['field_evidence']
    assert len(mapped['fragments']) == 22
    fixtures.require_multi_item_provenance({'provenance': p})


def test_explicit_multi_selection_requires_ninth_case_and_preserves_base(reports):
    gate, path, _, _ = reports
    assert len(acceptance_cases()) == 8
    assert gate.check(path)['status'] == 'passed'
    with pytest.raises(ValueError):
        gate.check(path, include_multi_item=True)
    gate, path, _, _ = expand_reports(reports)
    result = gate.check(path, include_multi_item=True)
    assert result['multi_item_evidence_checked'] is True and len(result['source_sha256']) == 5
    with pytest.raises(ValueError):
        gate.check(path)


@pytest.mark.parametrize('change', ['missing-case', 'duplicate-case', 'missing-step', 'missing-selection',
    'count', 'probe-count', 'seed-count', 'seed-sku', 'audit-missing', 'audit-row-count', 'missing-source', 'source-bytes'])
def test_expanded_gate_rejects_missing_scenario_counts_seed_audit_and_source(reports, change):
    gate, path, values, write = expand_reports(reports)
    run = values['roundtrip.json']
    if change == 'missing-case': run['operations'].pop()
    elif change == 'duplicate-case': run['operations'][-1] = deepcopy(run['operations'][0])
    elif change == 'missing-step': run['steps'].pop(-2)
    elif change == 'missing-selection': run.pop('include_multi_item')
    elif change == 'count': run['post_attempts'] = 8
    elif change == 'probe-count': values['permission-probes.json']['draft_documents_checked'] = 8
    elif change == 'seed-count': values['seed.json']['synthetic_item_count'] = True
    elif change == 'seed-sku': values['seed.json']['multi_skus'] = ['PF-SANDBOX-ITEM'] * 2
    elif change == 'audit-missing': values['database-audit.json'].pop('multi_item_components_independently_checked')
    elif change == 'audit-row-count': values['database-audit.json']['multi_item_row_count'] = 1
    elif change == 'missing-source': (path / 'input-fixtures' / fixtures.source_file(DATA)[0]).unlink()
    elif change == 'source-bytes': (path / 'input-fixtures' / fixtures.source_file(DATA)[0]).write_bytes(b'changed')
    write()
    with pytest.raises((ValueError, OSError)):
        gate.check(path, include_multi_item=True)


@pytest.mark.parametrize('field', ['multi_item', 'item_count', 'shipping_count', 'multi_item_provenance_verified',
    'duplicate_import_reused', 'preview_import_restart_verified', 'independent_readback_verified',
    'lost_receipt_reconciled', 'idempotent_replay_verified', 'single_post_verified', 'source_rows_verified',
    'supplier_id', 'transaction_date', 'contract_version', 'erp_cost_mapping_version'])
def test_every_multi_scenario_evidence_marker_required(reports, field):
    gate, path, values, write = expand_reports(reports)
    values['roundtrip.json']['operations'][-1].pop(field)
    write()
    with pytest.raises(ValueError):
        gate.check(path, include_multi_item=True)


@pytest.mark.parametrize('field', fixtures.LINE_FIELDS)
@pytest.mark.parametrize('mutation', ['missing', 'changed'])
def test_each_second_line_value_and_corresponding_evidence_required(reports, field, mutation):
    gate, path, values, write = expand_reports(reports)
    p = values['roundtrip.json']['operations'][-1]['provenance']
    if mutation == 'missing': p['quote_values']['lines'][1].pop(field)
    else: p['quote_values']['lines'][1][field] = 'mutated'
    write()
    with pytest.raises(ValueError, match='MULTI_ITEM_PROVENANCE'):
        gate.check(path, include_multi_item=True)


@pytest.mark.parametrize('change', ['missing-line', 'extra-line', 'duplicate-sku', 'missing-cell', 'wrong-row',
    'wrong-fragment', 'missing-shared-source', 'duplicate-shared-source', 'boolean-row'])
def test_multi_source_rows_are_complete_unique_and_exact(reports, change):
    gate, path, values, write = expand_reports(reports)
    p = values['roundtrip.json']['operations'][-1]['provenance']
    if change == 'missing-line': p['quote_values']['lines'].pop()
    elif change == 'extra-line': p['quote_values']['lines'].append(deepcopy(p['quote_values']['lines'][1]))
    elif change == 'duplicate-sku': p['quote_values']['lines'][1]['sku'] = p['quote_values']['lines'][0]['sku']
    elif change == 'missing-cell': p['field_evidence'].pop('lines.1.unit_price')
    elif change == 'wrong-row': p['field_evidence']['lines.1.unit_price']['row'] = 2
    elif change == 'wrong-fragment': p['field_evidence']['lines.1.unit_price']['fragment_id'] = p['field_evidence']['lines.0.unit_price']['fragment_id']
    elif change == 'missing-shared-source': p['field_evidence']['shipping_cost']['sources'].pop()
    elif change == 'duplicate-shared-source': p['field_evidence']['shipping_cost']['sources'][1] = deepcopy(p['field_evidence']['shipping_cost']['sources'][0])
    elif change == 'boolean-row': p['header_row'] = True
    write()
    with pytest.raises(ValueError, match='MULTI_ITEM_PROVENANCE'):
        gate.check(path, include_multi_item=True)


def test_workflow_explicitly_requires_multi_selection_in_runner_and_checker():
    source = (ROOT / '.github/workflows/erp-sandbox.yml').read_text()
    assert 'business-database: [sqlite, postgresql]' in source
    paths, = [line for line in source.splitlines() if line.lstrip().startswith('paths:')]
    assert "'apps/web/**'" in paths  # Final-head UI fixes also need both real ERP matrices.
    for script in ('verify_erp_sandbox.py', 'check_erp_sandbox_evidence.py'):
        command, = [line for line in source.splitlines() if f'python scripts/{script} ' in line]
        assert '--include-multi-item' in command


def test_real_import_helper_uses_both_rows_and_reuses_quote(system):
    client, _, erp = system
    def call(method, path, role='buyer', expected=200, **kwargs):
        response = client.request(method, '/api/v1' + path,
            headers={'Authorization': 'Bearer demo-' + role}, **kwargs)
        assert response.status_code == expected, response.text
        return response.json()
    request = call('POST', '/requests', expected=201, json={'title': 'Synthetic CI basket',
        'lines': [{key: row[key] for key in ('sku', 'quantity', 'uom')} for row in MULTI_ITEM_CASE['lines']],
        'budget': '100', 'max_delivery_days': 14})
    quote, report = fixtures.import_multi_item_quote(call, request['id'], {**DATA, 'supplier': 'SUP-A'}, lambda: None)
    fixtures.require_multi_item_provenance(report)
    assert len(quote['values']['lines']) == 2 and quote['calculation']['total'] == '47.05'
    assert erp.count() == 0
    confirmed = call('POST', f"/quotes/{quote['id']}/confirm",
        json={'expected_version': quote['version'], 'acknowledge': True})
    assert confirmed['confirmed_by'] == 'buyer-01' and confirmed['evidence'] == quote['evidence']
    proposal = call('POST', f"/requests/{request['id']}/analyze", json={})['proposal']
    assert proposal['total'] == '47.05' and proposal['contract_version'] == 'multi-sku-v1'
    call('POST', f"/requests/{request['id']}/approval", role='approver',
        json={'snapshot_hash': proposal['snapshot_hash']})
    operation = call('POST', f"/requests/{request['id']}/execute", expected=202,
        json={'snapshot_hash': proposal['snapshot_hash']})
    assert call('POST', f"/operations/{operation['id']}/process")['status'] == 'COMPLETED'
    receipt = call('POST', f"/operations/{operation['id']}/verify", role='auditor')
    assert receipt['status'] == 'verified' and receipt['simulated'] is True
    assert erp.count() == 1 and len(erp.find(operation['id'])['lines']) == 2


def test_complete_nine_case_http_worker_harness_is_offline_erp_double(monkeypatch):
    import test_erp_tabular_acceptance as legacy
    spec = importlib.util.spec_from_file_location('pf_multi_harness', ROOT / 'scripts/verify_erp_sandbox.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    original = legacy.persisted_cost
    def persisted(body, number):
        if len(body['items']) != 2:
            return original(body, number)
        record = deepcopy(body)
        record.update(name=f'SQ-{number}', total='41.80', net_total='41.80', grand_total='47.05', total_taxes_and_charges='5.25')
        amounts = {'PF-SANDBOX-ITEM': '20.50', 'PF-SANDBOX-ITEM-2': '21.30'}
        for item in record['items']:
            item.update(amount=amounts[item['item_code']], net_amount=amounts[item['item_code']], net_rate=str(item['rate']))
        record['taxes'][0].update(rate=None, tax_amount_after_discount_amount='5.25', total='47.05')
        return record
    monkeypatch.setattr(legacy, 'persisted_cost', persisted)
    def raise_failure(error):
        raise error
    monkeypatch.setattr(runner, 'safe_failure', raise_failure)
    with legacy.offline_erp_server() as (url, state):
        monkeypatch.setattr(runner, 'TARGET', url)
        result = runner.exercise(legacy.DATA, include_multi_item=True)
    assert result['status'] == 'passed' and result['include_multi_item'] is True
    assert result['post_attempts'] == len(state['posts']) == len(state['documents']) == 9
    assert not state['unexpected']
    op = result['operations'][-1]
    fixtures.require_multi_item_provenance(op)
    assert op['scenario'] == MULTI_ITEM_SCENARIO and op['item_count'] == 2 and op['single_post_verified']


@pytest.mark.parametrize('backend', ['sqlite', 'postgresql'])
def test_selected_nine_case_evidence_requires_exact_business_database_counts(reports, backend):
    gate, path, values, write = expand_reports(reports)
    if backend == 'postgresql':
        values['roundtrip.json']['api_business_database'] = 'PostgreSQL'
        values['roundtrip.json']['business_database_audit'] = {
            'status': 'passed', 'database': 'PostgreSQL', 'server_version_num': '170006',
            **{key: True for key in ('isolated_schema', 'migration_current', 'operation_identity_matches',
                'read_only_audit', 'after_api_restart')},
            **{key: 9 for key in ('operation_count', 'completed_operation_count', 'outbox_count',
                'done_outbox_count', 'verified_receipt_count')}}
        write()
    assert gate.check(path, backend, include_multi_item=True)['status'] == 'passed'
    if backend == 'postgresql':
        values['roundtrip.json']['business_database_audit']['operation_count'] = 8
        write()
        with pytest.raises(ValueError, match='BUSINESS_AUDIT_INVALID_COUNTS'):
            gate.check(path, backend, include_multi_item=True)


@pytest.mark.parametrize('mutate', [False, True])
def test_permission_probes_preserve_seven_denials_and_read_all_nine_drafts(monkeypatch, mutate):
    import test_erp_permission_probes as legacy
    from test_erp_multi_cost_audit import persisted_document
    data, operations = legacy.inputs()
    data['multi_skus'] = ['PF-SANDBOX-ITEM', 'PF-SANDBOX-ITEM-2']
    operations.append({'remote_id': 'SQ-MULTI', 'operation_id': 'b' * 64, 'snapshot_hash': 'c' * 64,
        'expected_total': '47.05', 'scenario': MULTI_ITEM_SCENARIO})
    original = legacy.draft
    def draft(data, operation):
        if operation['scenario'] != MULTI_ITEM_SCENARIO:
            return original(data, operation)
        doc = persisted_document()
        doc.update(doctype='Supplier Quotation', name=operation['remote_id'], supplier=data['supplier'],
            custom_procureflow_operation_key=operation['operation_id'],
            custom_procureflow_snapshot_hash=operation['snapshot_hash'], transaction_date='2026-10-08')
        if mutate:
            doc['items'].pop()
        return doc
    monkeypatch.setattr(legacy, 'inputs', lambda: (data, operations))
    monkeypatch.setattr(legacy, 'draft', draft)
    transport, calls, negatives = legacy.fake_erp()
    result = legacy.probe.exercise(data, operations, transport=transport)
    if mutate:
        assert result['status'] == 'failed' and result['reason'] == 'DRAFT_FIXTURE_NOT_VERIFIED'
        assert not negatives
    else:
        assert result['status'] == 'passed' and result['draft_documents_checked'] == 9
        assert len(negatives) == 7 and len(calls) == 28
        legacy.probe.require_probe_evidence(result, include_multi_item=True)
        with pytest.raises(ValueError):
            legacy.probe.require_probe_evidence(result)


def test_probe_input_validation_expands_only_explicit_complete_nine_case_records(tmp_path):
    import test_erp_permission_probes as legacy
    credentials, env, roundtrip = legacy.write_inputs(tmp_path)
    data = json.loads(credentials.read_text()); run = json.loads(roundtrip.read_text())
    data['multi_skus'] = ['PF-SANDBOX-ITEM', 'PF-SANDBOX-ITEM-2']
    run.update(include_multi_item=True)
    run['operations'].append({'remote_id': 'SQ-MULTI', 'operation_id': 'b' * 64,
        'snapshot_hash': 'c' * 64, 'expected_total': '47.05', 'scenario': MULTI_ITEM_SCENARIO})
    credentials.write_text(json.dumps(data)); roundtrip.write_text(json.dumps(run))
    assert len(legacy.probe.validate_inputs(credentials, env, roundtrip)[1]) == 9
    run.pop('include_multi_item'); roundtrip.write_text(json.dumps(run))
    with pytest.raises(ValueError):
        legacy.probe.validate_inputs(credentials, env, roundtrip)


from test_erp_acceptance_audit import audit_module


@pytest.mark.parametrize('mutation', [None, 'duplicate-second-row', 'wrong-supplier', 'wrong-date'])
def test_mariadb_audit_dispatches_selected_ninth_draft_to_independent_checker(audit_module, monkeypatch, mutation):
    from types import SimpleNamespace
    from test_erp_multi_cost_audit import persisted_document
    module, path, operations, rows, checks, _ = audit_module
    operation = {'scenario': MULTI_ITEM_SCENARIO, 'operation_id': 'multi-op', 'remote_id': 'SQ-MULTI',
        'snapshot_hash': 'a' * 64, 'supplier_id': 'Synthetic Supplier', 'transaction_date': '2026-10-08'}
    operations.append(operation)
    rows.append(SimpleNamespace(name='SQ-MULTI', docstatus=0, grand_total='47.05',
        custom_procureflow_operation_key='multi-op', custom_procureflow_snapshot_hash='a' * 64))
    path.write_text(json.dumps({'include_multi_item': True, 'operations': operations}))
    document = persisted_document()
    document.update(supplier='Synthetic Supplier', transaction_date='2026-10-08')
    if mutation == 'duplicate-second-row': document['items'][1] = deepcopy(document['items'][0])
    elif mutation == 'wrong-supplier': document['supplier'] = 'Other'
    elif mutation == 'wrong-date': document['transaction_date'] = '2026-10-09'
    monkeypatch.setattr(module.frappe, 'get_doc', lambda kind, name: SimpleNamespace(
        as_dict=lambda: document if name == 'SQ-MULTI' else {'name': name}))
    if mutation:
        with pytest.raises(AssertionError):
            module.main()
    else:
        result = module.main()
        assert result['draft_count'] == 9 and result['multi_item_row_count'] == 2
        assert result['multi_item_draft_count'] == 1 and result['multi_item_components_independently_checked']
        assert len(checks) == 8
