"""Offline contract tests. These do not claim real Frappe role enforcement."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    'pf_account_reference', ROOT / 'integrations/erpnext/sandbox/lab_permissions.py')
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


def permissions():
    return {right: right == 'select' for right in lab.ACCOUNT_RIGHTS}


def write_records(directory):
    for name, stage in [('seed.json', 'seed'), ('database-audit.json', 'database-audit')]:
        (directory / name).write_text(json.dumps({
            'stage': stage, 'status': 'passed', 'reference_permissions': permissions()}))


def test_select_only_queries_actual_account_as_integration_user():
    calls = []
    def check(doctype, right, **kwargs):
        calls.append((doctype, right, kwargs))
        return right == 'select'
    assert lab.verify_account_reference(check, 'pf-integration@example.invalid', 'Synthetic Payable') == permissions()
    assert [call[1] for call in calls] == list(lab.ACCOUNT_RIGHTS)
    assert all(call[0] == 'Account' and call[2] == {
        'doc': 'Synthetic Payable', 'user': 'pf-integration@example.invalid'} for call in calls)


@pytest.mark.parametrize('right', lab.ACCOUNT_RIGHTS)
def test_every_missing_or_escalated_right_is_rejected(right):
    observed = permissions()
    observed[right] = not observed[right]
    with pytest.raises(ValueError, match='NOT_SELECT_ONLY'):
        lab.verify_account_reference(lambda dt, p, **kw: observed[p],
                                     'pf-integration@example.invalid', 'Synthetic Payable')


@pytest.mark.parametrize('account', [None, '', ' ', 1])
def test_missing_account_stops_before_permission_query(account):
    def unexpected(*args, **kwargs):
        pytest.fail('must fail before querying permissions')
    with pytest.raises(ValueError, match='CONTEXT_INVALID'):
        lab.verify_account_reference(unexpected, 'pf-integration@example.invalid', account)


def test_administrator_is_not_a_valid_permission_probe_identity():
    with pytest.raises(ValueError, match='CONTEXT_INVALID'):
        lab.verify_account_reference(lambda *a, **k: pytest.fail('unexpected probe'), 'Administrator', 'account')


@pytest.mark.parametrize('bad', [None, {}, [], {'select': True},
    {**permissions(), 'read': 0}, {**permissions(), 'select': 1},
    {**permissions(), 'read': 'false'}, {**permissions(), 'extra': False}])
def test_incomplete_or_coerced_evidence_cannot_pass(bad):
    with pytest.raises(ValueError):
        lab.require_select_only(bad)


def test_complete_seed_and_audit_reference_evidence_pass(tmp_path):
    write_records(tmp_path)
    result = lab.check_records(tmp_path)
    assert result['status'] == 'passed' and result['account_select_only'] is True
    assert set(result['record_sha256']) == {'seed.json', 'database-audit.json'}
    assert all(len(value) == 64 for value in result['record_sha256'].values())


@pytest.mark.parametrize('name', ['seed.json', 'database-audit.json'])
@pytest.mark.parametrize('mutation', ['missing', 'status', 'stage', 'permissions', 'oversized', 'duplicate'])
def test_either_stage_missing_invalid_or_escalated_blocks_acceptance(tmp_path, name, mutation):
    write_records(tmp_path)
    path = tmp_path / name
    record = json.loads(path.read_text())
    if mutation == 'missing':
        path.unlink()
    elif mutation == 'oversized':
        path.write_text(' ' * (lab.MAX_RECORD_BYTES + 1))
    elif mutation == 'duplicate':
        path.write_text('{"status":"failed","status":"passed"}')
    else:
        if mutation == 'status': record['status'] = 'failed'
        if mutation == 'stage': record['stage'] = 'other'
        if mutation == 'permissions': record['reference_permissions']['read'] = True
        path.write_text(json.dumps(record))
    with pytest.raises((ValueError, OSError)):
        lab.check_records(tmp_path)


def test_cli_failure_does_not_echo_untrusted_evidence(tmp_path, capsys):
    (tmp_path / 'seed.json').write_text('PRIVATE_CREDENTIAL_MUST_NOT_BE_ECHOED')
    assert lab.main(['--directory', str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert 'PRIVATE_CREDENTIAL' not in output.out + output.err
    assert str(tmp_path) not in output.out + output.err
    assert json.loads(output.out)['reason'] == 'REFERENCE_PERMISSION_EVIDENCE_INVALID'


def test_seed_grants_select_without_account_read_or_mutation():
    source = (ROOT / 'integrations/erpnext/sandbox/seed.py').read_text()
    tree = ast.parse(source)
    grants = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == 'add_permission'
              and node.args and isinstance(node.args[0], ast.Constant)
              and node.args[0].value == 'Account']
    assert len(grants) == 1
    assert [(kw.arg, ast.literal_eval(kw.value)) for kw in grants[0].keywords] == [('ptype', 'select')]
    assert 'verify_account_reference(frappe.has_permission, USER, account)' in source
    audit = (ROOT / 'integrations/erpnext/sandbox/audit.py').read_text()
    assert 'verify_account_reference(frappe.has_permission, USER, account)' in audit
    assert 'ignore_permissions=True' not in source


@pytest.mark.parametrize('implicit_read,implicit_export', [(True, True), (True, False), (False, True), (False, False)])
def test_seed_revokes_frappe_implicit_defaults_without_disabling_validation(implicit_read, implicit_export):
    # Execute the actual seed configuration statements against a default-aware
    # permission store. A fake that defaults every omitted flag to 0 hid this bug.
    tree = ast.parse((ROOT / 'integrations/erpnext/sandbox/seed.py').read_text())
    statements = sorted((node for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {'add_permission', 'update_permission_property'}
        and node.value.args and isinstance(node.value.args[0], ast.Constant)
        and node.value.args[0].value == 'Account'), key=lambda node: node.lineno)
    stored = {}
    events = []
    def add(doctype, role, *, ptype):
        assert doctype == 'Account' and role == 'Restricted Test Role' and ptype == 'select'
        stored.update({right: False for right in lab.ACCOUNT_RIGHTS})
        stored.update(read=implicit_read, export=implicit_export, select=True)
        events.append(('grant', ptype))
    def update(doctype, role, level, right, value, validate=True):
        assert doctype == 'Account' and role == 'Restricted Test Role' and level == 0
        assert validate is True and value == 0
        stored[right] = bool(value)
        events.append(('revoke', right))
    code = compile(ast.Module(body=statements, type_ignores=[]), '<seed-account-configuration>', 'exec')
    exec(code, {'add_permission': add, 'update_permission_property': update, 'ROLE': 'Restricted Test Role'})
    assert events == [('grant', 'select'), ('revoke', 'export'), ('revoke', 'read')]
    assert lab.require_select_only(stored) == permissions()
