"""Offline acceptance/audit controls; these do not claim a live ERP execution."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
SANDBOX = ROOT / 'integrations/erpnext/sandbox'
sys.path.insert(0, str(SANDBOX))
from cost_fixtures import ACCEPTANCE_CASES, COST_CASES

SCENARIOS = ['normal', 'lost-receipt', 'excluded-discount', 'included-discount',
    'csv-excluded-discount', 'csv-included-discount-lost-receipt',
    'xlsx-excluded-discount', 'xlsx-included-discount-lost-receipt']


def test_acceptance_matrix_preserves_original_four_independent_cost_cases():
    assert list(COST_CASES) == SCENARIOS[:4]
    assert list(ACCEPTANCE_CASES) == SCENARIOS
    assert len({id(case) for case in ACCEPTANCE_CASES.values()}) == 8
    for index, scenario in enumerate(SCENARIOS):
        accepted = ACCEPTANCE_CASES[scenario]
        cost_scenario = scenario if index < 4 else ('included-discount' if index % 2 else 'excluded-discount')
        assert accepted == {**COST_CASES[cost_scenario],
            'input_format': 'txt' if index < 4 else ('csv' if index < 6 else 'xlsx'),
            'lose_receipt': index in {1, 5, 7}}
        assert set(COST_CASES[cost_scenario]).isdisjoint({'input_format', 'lose_receipt'})


@pytest.fixture
def audit_module(tmp_path, monkeypatch):
    operations = [{'scenario': scenario, 'operation_id': f'operation-{index}',
        'remote_id': f'SQ-{index}', 'snapshot_hash': f'{index:064x}'}
        for index, scenario in enumerate(SCENARIOS)]
    path = tmp_path / 'roundtrip.json'
    path.write_text(json.dumps({'operations': operations}))
    rows = [SimpleNamespace(name=op['remote_id'], docstatus=0,
        grand_total=ACCEPTANCE_CASES[op['scenario']]['total'],
        custom_procureflow_operation_key=op['operation_id'],
        custom_procureflow_snapshot_hash=op['snapshot_hash']) for op in operations]
    calls, cost_checks = [], []

    def sql(statement, *args, **kwargs):
        calls.append(statement)
        if statement.startswith('SHOW INDEX'):
            return [SimpleNamespace(Column_name='custom_procureflow_operation_key', Non_unique=0)]
        if statement.startswith('UPDATE'):
            raise ValueError('1062 Duplicate entry')
        if statement == 'SELECT VERSION()':
            return [['synthetic-database-version']]
        return []

    db = SimpleNamespace(sql=sql, count=lambda kind: 0,
        rollback=lambda: calls.append('rollback'),
        get_value=lambda kind, name, field: 'CNY' if field == 'default_currency' else 'Synthetic Payable',
        get_single_value=lambda kind, field: {'currency_precision': '2', 'float_precision': '6',
            'rounding_method': 'Commercial Rounding'}[field])
    frappe = SimpleNamespace(db=db, __version__='synthetic-frappe-version',
        get_all=lambda *args, **kwargs: rows,
        get_doc=lambda kind, name: SimpleNamespace(as_dict=lambda: {'name': name}),
        has_permission=lambda kind, permission, **kwargs: kind == 'Account' and permission == 'select')
    monkeypatch.setitem(sys.modules, 'frappe', frappe)
    monkeypatch.setitem(sys.modules, 'erpnext', SimpleNamespace(__version__='synthetic-erpnext-version'))
    monkeypatch.setitem(sys.modules, 'seed', SimpleNamespace(COMPANY='ProcureFlow Sandbox', USER='pf-integration@example.invalid'))
    monkeypatch.setitem(sys.modules, 'lab_runtime', SimpleNamespace(lab_session=nullcontext,
        phase=lambda name: calls.append(name), run_stage=lambda *args: None))
    spec = importlib.util.spec_from_file_location('erp_acceptance_audit_test', SANDBOX / 'audit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'Path', lambda name: path)
    monkeypatch.setattr(module, 'verify_cost_document', lambda document, expected: cost_checks.append((document, expected)))
    return module, path, operations, rows, cost_checks, calls


def test_database_audit_independently_checks_every_ordered_acceptance_case(audit_module):
    module, _, operations, _, checks, calls = audit_module
    result = module.main()
    assert result['status'] == 'passed' and result['draft_count'] == 8
    assert result['purchase_order_count'] == result['submitted_count'] == 0
    assert checks == [({'name': op['remote_id']}, ACCEPTANCE_CASES[op['scenario']]) for op in operations]
    assert 'ROLLBACK TO SAVEPOINT pf_unique_probe' in calls and calls[-1] == 'rollback'
    assert result['cost_components_independently_checked'] is True
    assert result['duplicate_key_update_rejected_and_rolled_back'] is True


@pytest.mark.parametrize('mutation', ['old-four', 'missing', 'extra', 'reorder', 'duplicate', 'unknown'])
def test_database_audit_rejects_incomplete_or_reordered_acceptance_cases(audit_module, mutation):
    module, path, operations, _, checks, _ = audit_module
    if mutation == 'old-four': operations = operations[:4]
    elif mutation == 'missing': operations.pop()
    elif mutation == 'extra': operations.append(dict(operations[-1]))
    elif mutation == 'reorder': operations[4:6] = reversed(operations[4:6])
    elif mutation == 'duplicate': operations[7]['scenario'] = operations[6]['scenario']
    elif mutation == 'unknown': operations[7]['scenario'] = 'other'
    path.write_text(json.dumps({'operations': operations}))
    with pytest.raises(AssertionError): module.main()
    assert checks == []


@pytest.mark.parametrize('mutation', ['missing-draft', 'submitted', 'wrong-total', 'wrong-snapshot', 'duplicate-operation'])
def test_new_tabular_drafts_are_subject_to_existing_database_guards(audit_module, mutation):
    module, _, _, rows, _, _ = audit_module
    if mutation == 'missing-draft': rows.pop()
    elif mutation == 'submitted': rows[-1].docstatus = 1
    elif mutation == 'wrong-total': rows[-1].grand_total = '2227.01'
    elif mutation == 'wrong-snapshot': rows[-1].custom_procureflow_snapshot_hash = 'unrelated'
    elif mutation == 'duplicate-operation': rows[-1].custom_procureflow_operation_key = rows[-2].custom_procureflow_operation_key
    with pytest.raises(AssertionError): module.main()
