"""Public sandbox diagnostics must not leak remote bodies or certify failed runs."""
import importlib.util
import json
from pathlib import Path
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


def runner():
    spec = importlib.util.spec_from_file_location('erp_diagnostic_test', ROOT / 'scripts/verify_erp_sandbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('path,family', [
    ('/api/resource/Custom Field/private-canary', 'Custom Field'),
    ('/api/resource/Company/private-canary', 'Company'),
    ('/api/resource/Supplier Quotation/private-canary', 'Supplier Quotation'),
    ('/api/method/frappe.auth.get_logged_user', 'identity'),
])
def test_http_observation_only_exposes_fixed_categories(path, family):
    response = httpx.Response(403, json={'exc_type': 'PermissionError', 'exception': 'private-canary',
        'api_key': 'private-canary', 'exc': 'private-canary'})
    result = runner().http_observation('GET', path, response)
    assert result == {'method': 'GET', 'endpoint': family, 'status': 403, 'error_type': 'PermissionError'}
    assert 'private-canary' not in json.dumps(result)


def test_unknown_error_class_and_exception_message_not_exported():
    module = runner()
    result = module.http_observation('GET', '/api/resource/unknown-canary',
        httpx.Response(403, json={'exc_type': 'SECRET_CANARY'}))
    assert result == {'method': 'GET', 'endpoint': 'other', 'status': 403}
    assert module.safe_failure(RuntimeError('SECRET_CANARY')) == 'BUSINESS_ROUNDTRIP_FAILED'
    assert module.safe_failure(RuntimeError('ERP_HTTP_403')) == 'ERP_HTTP_403'


@pytest.mark.parametrize('status,code', [('failed', 1), ('passed', 0)])
def test_cli_preserves_failure_exit_status(tmp_path, monkeypatch, status, code):
    module = runner()
    monkeypatch.setattr(module, 'validate_lab', lambda *_: {})
    monkeypatch.setattr(module, 'exercise', lambda *_: {'status': status})
    output = tmp_path / 'report.json'
    assert module.main(['--ephemeral-test', '--output', str(output)]) == code
    assert json.loads(output.read_text())['status'] == status


def test_remote_trace_exposes_only_allowlisted_locations_and_type_names():
    module = runner()
    raw = 'File "/SECRET_CANARY/controllers/get_item_details.py", line 123, in private\n  SECRET_CANARY\nFile "/SECRET_CANARY/SECRET_CANARY.py", line 9, in hidden'
    body = {'exc': json.dumps([raw]), '_server_messages': 'No permission for Item Price SECRET_CANARY'}
    result = module.remote_error_details(body)
    assert result['source_locations'] == [{'file': 'get_item_details.py', 'line': 123}]
    assert 'Item Price' in result['mentioned_doctypes']
    assert 'SECRET_CANARY' not in json.dumps(result)


def test_remote_trace_rejects_oversized_or_malformed_values():
    module = runner()
    assert module.remote_error_details({'exc': 'x'*65537, '_server_messages': 'x'*16385}) == {}
    assert module.remote_error_details({'exc': {}, '_server_messages': []}) == {}
    assert module.remote_error_details({'exc': json.dumps([{}])}) == {}


@pytest.mark.parametrize('field', ['supplier', 'company', 'currency', 'transaction_date',
    'custom_procureflow_operation_key', 'custom_procureflow_snapshot_hash', 'item_code', 'uom', 'qty', 'rate', 'grand_total'])
def test_readback_diagnostics_expose_only_fixed_boolean_field_checks(field):
    module = runner()
    expected = {'supplier': 'PRIVATE_SUPPLIER', 'company': 'PRIVATE_COMPANY', 'currency': 'CNY',
        'transaction_date': '2026-01-01', 'custom_procureflow_operation_key': 'PRIVATE_KEY',
        'custom_procureflow_snapshot_hash': 'PRIVATE_HASH',
        'items': [{'item_code': 'PRIVATE_ITEM', 'uom': 'EA', 'qty': '20', 'rate': '100.00'}]}
    remote = json.loads(json.dumps(expected))
    remote.update(docstatus=0, grand_total=2000)
    assert all(module.document_contract_observation(expected, httpx.Response(200, json={'data': remote})).values())
    if field in {'qty', 'rate'}:
        remote['items'][0][field] = 1
    elif field in {'item_code', 'uom'}:
        remote['items'][0][field] = 'PRIVATE_DIFFERENT'
    elif field == 'grand_total':
        remote[field] = 1
    else:
        remote[field] = 'PRIVATE_DIFFERENT'
    result = module.document_contract_observation(expected, httpx.Response(200, json={'data': remote}))
    assert result[field + '_matches'] is False
    assert all(type(value) is bool for value in result.values())
    assert 'PRIVATE_' not in json.dumps(result)


@pytest.mark.parametrize('body', [None, [], {}, {'data': None}, {'data': {'items': []}}, {'data': {'items': [None]}}])
def test_readback_diagnostics_reject_malformed_shape_without_echo(body):
    assert runner().document_contract_observation({}, httpx.Response(200, json=body)) == {'document_shape_valid': False}


def test_safe_failure_retains_known_readback_code_only():
    module = runner()
    assert module.safe_failure(RuntimeError('ERP_READBACK_PAYLOAD_MISMATCH')) == 'ERP_READBACK_PAYLOAD_MISMATCH'
    assert module.safe_failure(RuntimeError('ERP_READBACK_PAYLOAD_MISMATCH PRIVATE_CANARY')) == 'BUSINESS_ROUNDTRIP_FAILED'
