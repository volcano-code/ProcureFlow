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
