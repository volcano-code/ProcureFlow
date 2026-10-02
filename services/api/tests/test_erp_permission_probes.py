"""Offline runner contracts; only disposable ERP CI proves real REST enforcement."""
import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('pf_permission_probes', ROOT / 'scripts/probe_erp_permissions.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
CANARY = 'PRIVATE_CANARY_DO_NOT_PUBLISH'


def inputs():
    data = {'site': 'pf-erp-test.local', 'company': 'ProcureFlow Sandbox',
        'user': 'pf-integration@example.invalid', 'sku': 'PF-SANDBOX-ITEM',
        'supplier': 'PF Synthetic Supplier', 'nonce': 'a' * 64,
        'api_key': CANARY + '_KEY', 'api_secret': CANARY + '_SECRET'}
    operations = [{'remote_id': f'PUR-SQTN-2026-0000{idx + 1}', 'operation_id': c * 64,
        'snapshot_hash': d * 64, 'expected_total': probe.COST_CASES[scenario]['total'], 'scenario': scenario}
        for idx, (scenario, c, d) in enumerate(zip(probe.COST_CASES, 'abcd', 'cdef', strict=True))]
    return data, operations


def write_inputs(directory):
    data, operations = inputs()
    credentials, env_file, roundtrip = (directory / name for name in ('credentials.json', 'test.env', 'roundtrip.json'))
    credentials.write_text(json.dumps(data))
    env_file.write_text('PF_EPHEMERAL_NONCE=' + data['nonce'] + '\n')
    roundtrip.write_text(json.dumps({'status': 'passed', 'synthetic_only': True,
        'real_user_account_used': False, 'operations': operations}))
    return credentials, env_file, roundtrip


def draft(data, operation):
    return {'doctype': 'Supplier Quotation', 'name': operation['remote_id'], 'docstatus': 0,
        'company': data['company'], 'supplier': data['supplier'], 'currency': 'CNY',
        'custom_procureflow_operation_key': operation['operation_id'],
        'custom_procureflow_snapshot_hash': operation['snapshot_hash'], 'grand_total': operation['expected_total'],
        'transaction_date': '2026-09-30', 'modified': '2026-09-30 00:00:00',
        'items': [{'item_code': data['sku'], 'uom': 'EA', 'qty': 20, 'rate': probe.COST_CASES[operation['scenario']]['unit_price']}]}


def fake_erp(*, changed=None, identity=None, bad_account_selection=False, failed_probe=None,
             denied_response=None, draft_override=None):
    data, operations = inputs()
    calls, negative_calls, read_counts = [], [], {}
    account = CANARY + '_PAYABLE - PFL'
    def handler(request):
        calls.append(request)
        assert request.url.scheme == 'http' and request.url.host == '127.0.0.1' and request.url.port == 18080
        assert request.headers['Authorization'] == f"token {data['api_key']}:{data['api_secret']}"
        path = request.url.path
        if path == '/api/method/frappe.auth.get_logged_user':
            return httpx.Response(200, json={'message': data['user'] if identity is None else identity})
        if path == '/api/resource/Company/' + data['company']:
            return httpx.Response(200, json={'data': {'name': data['company'], 'company_name': data['company'],
                'abbr': 'PFL', 'default_payable_account': account}})
        if path == '/api/resource/Account':
            assert request.method == 'GET'
            assert json.loads(request.url.params['fields']) == ['name']
            assert json.loads(request.url.params['filters']) == {'name': account}
            return httpx.Response(200, json={'data': [] if bad_account_selection else [{'name': account}]})
        if request.method == 'GET' and path.startswith('/api/resource/Supplier Quotation/'):
            name = path.rsplit('/', 1)[1]
            operation = next(op for op in operations if op['remote_id'] == name)
            doc = draft(data, operation)
            if draft_override:
                doc.update(draft_override)
            read_counts[name] = read_counts.get(name, 0) + 1
            if changed and read_counts[name] > 1:
                doc[changed] = 'unexpected-change' if changed == 'modified' else 1
            return httpx.Response(200, json={'data': doc})
        index = len(negative_calls)
        expected = probe.PROBES[index]
        assert request.method == expected[1]
        assert path.startswith('/api/resource/' + expected[2])
        negative_calls.append(request)
        if index == failed_probe:
            return denied_response or httpx.Response(200, json={'data': CANARY})
        return httpx.Response(403, json={'exc_type': 'PermissionError', 'exception': CANARY,
            '_server_messages': CANARY, 'exc': CANARY, 'secret': CANARY})
    return httpx.MockTransport(handler), calls, negative_calls


def passed_record():
    return {'stage': 'permission-probes', 'status': 'passed', 'real_user_account_used': False,
        'draft_documents_checked': 4, **dict.fromkeys(probe.REQUIRED_TRUE, True),
        'probes': [{'name': name, 'method': method, 'endpoint': endpoint, 'status': 403,
            'error_type': 'PermissionError', 'permission_denied': True} for name, method, endpoint in probe.PROBES]}


def test_all_realistic_rest_denials_and_positive_controls_are_required():
    transport, calls, negatives = fake_erp()
    data, operations = inputs()
    result = probe.exercise(data, operations, transport=transport)
    assert result['status'] == 'passed'
    assert probe.require_probe_evidence(result) is result
    assert len(negatives) == 7 and len(calls) == 18
    assert json.loads(negatives[3].content) == {'docstatus': 1}
    assert json.loads(negatives[1].content) == {'disabled': 0}
    po = json.loads(negatives[-1].content)
    assert po['doctype'] == 'Purchase Order' and po['docstatus'] == 0
    assert po['company'] == data['company'] and po['supplier'] == data['supplier']
    assert po['items'] == [{'item_code': data['sku'], 'uom': 'EA', 'qty': 20, 'rate': 100,
                            'schedule_date': '2026-09-30'}]
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (CANARY, data['supplier'], data['api_key'],
        data['api_secret'], operations[0]['remote_id'], operations[0]['operation_id'], data['nonce']))


@pytest.mark.parametrize('status,body', [
    (200, {'exc_type': 'PermissionError'}),
    (201, {'data': CANARY}), (202, {'message': 'ok'}),
    (400, {'exc_type': 'PermissionError'}), (401, {'exc_type': 'AuthenticationError'}),
    (403, {'exc_type': 'AuthenticationError'}), (403, {'exc_type': 'ValidationError'}),
    (403, {'exc_type': 'DoesNotExistError'}), (403, {'exception': 'frappe.PermissionError'}),
    (403, {'exc_type': ['PermissionError']}), (403, []),
    (404, {'exc_type': 'DoesNotExistError'}), (417, {'exc_type': 'MandatoryError'}),
    (417, {'exc_type': 'LinkValidationError'}), (417, {'exc_type': 'DocstatusTransitionError'}),
    (500, {'exc_type': 'PermissionError'}), (302, {'exc_type': 'PermissionError'}),
])
def test_invalid_auth_validation_notfound_and_success_are_never_permission_passes(status, body):
    response = httpx.Response(status, json=body)
    # Include every probe position so no later failure can be hidden by earlier passes.
    for index in range(len(probe.PROBES)):
        transport, _, negatives = fake_erp(failed_probe=index, denied_response=response)
        result = probe.exercise(*inputs(), transport=transport)
        assert result['status'] == 'failed' and len(negatives) == index + 1
        assert result['probes'][-1]['permission_denied'] is False
        assert CANARY not in json.dumps(result)


@pytest.mark.parametrize('content', [b'Forbidden', b'<h1>403</h1>', b'{"exc_type":"PermissionError","exc_type":"PermissionError"}',
    b'{"exc_type":"PermissionError","value":NaN}', b'x' * (probe.MAX_RESPONSE_BYTES + 1)])
def test_denials_require_bounded_strict_structured_json(content):
    observed = probe.denial_observation(probe.PROBES[0], httpx.Response(403, content=content))
    assert observed['permission_denied'] is False
    assert set(observed) == {'name', 'method', 'endpoint', 'status', 'permission_denied'}


@pytest.mark.parametrize('kwargs,reason', [
    ({'identity': 'Administrator'}, 'IDENTITY_NOT_VERIFIED'),
    ({'identity': 'Guest'}, 'IDENTITY_NOT_VERIFIED'),
    ({'bad_account_selection': True}, 'ACCOUNT_SELECT_NOT_VERIFIED'),
    ({'draft_override': {'docstatus': 1}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
    ({'draft_override': {'docstatus': False}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
    ({'draft_override': {'supplier': 'unrelated'}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
    ({'draft_override': {'custom_procureflow_operation_key': 'unrelated'}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
    ({'draft_override': {'items': []}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
    ({'draft_override': {'grand_total': 1}}, 'DRAFT_FIXTURE_NOT_VERIFIED'),
])
def test_identity_and_existing_fixture_controls_block_all_negative_actions(kwargs, reason):
    transport, _, negatives = fake_erp(**kwargs)
    result = probe.exercise(*inputs(), transport=transport)
    assert result['status'] == 'failed' and result['reason'] == reason
    assert negatives == [] and result['probes'] == []


@pytest.mark.parametrize('changed', ['modified', 'docstatus', 'grand_total'])
def test_denial_responses_cannot_hide_changed_or_submitted_drafts(changed):
    transport, _, negatives = fake_erp(changed=changed)
    result = probe.exercise(*inputs(), transport=transport)
    assert len(negatives) == 7 and result['status'] == 'failed'
    assert result.get('drafts_unchanged') is not True


def test_transport_failure_is_bounded_and_does_not_expose_request_or_credentials():
    def handler(request):
        raise httpx.ConnectError(CANARY, request=request)
    result = probe.exercise(*inputs(), transport=httpx.MockTransport(handler))
    assert result['status'] == 'failed' and result['reason'] == 'ERP_TRANSPORT_FAILED'
    assert CANARY not in json.dumps(result)


def test_redirect_is_not_followed_or_treated_as_a_denial():
    transport, calls, negatives = fake_erp(failed_probe=0,
        denied_response=httpx.Response(302, headers={'Location': 'https://production.invalid/private'},
                                      json={'exc_type': 'PermissionError'}))
    result = probe.exercise(*inputs(), transport=transport)
    assert result['status'] == 'failed' and len(negatives) == 1
    assert all(request.url.host == '127.0.0.1' for request in calls)
    assert result['probes'][0]['permission_denied'] is False


def test_valid_inputs_use_matching_seed_and_roundtrip(tmp_path):
    paths = write_inputs(tmp_path)
    assert probe.validate_inputs(*paths) == inputs()


@pytest.mark.parametrize('field,value', [
    ('site', 'production'), ('company', 'Actual Business'), ('user', 'Administrator'),
    ('sku', 'ACTUAL'), ('nonce', 'b' * 64), ('nonce', 'g' * 64), ('nonce', []),
    ('api_key', ''), ('api_secret', 'whitespace token'), ('supplier', '../outside'),
])
def test_nonfixture_credentials_cannot_enable_network(tmp_path, monkeypatch, field, value):
    paths = write_inputs(tmp_path)
    data = json.loads(paths[0].read_text())
    data[field] = value
    paths[0].write_text(json.dumps(data))
    monkeypatch.setattr(probe, 'exercise', lambda *args: pytest.fail('network must remain blocked'))
    out = tmp_path / 'report.json'
    assert probe.main(['--ephemeral-test', '--credentials', str(paths[0]), '--env-file', str(paths[1]),
                       '--roundtrip', str(paths[2]), '--output', str(out)]) == 2
    result = json.loads(out.read_text())
    assert result['status'] == 'blocked' and result['network_attempted'] is False


@pytest.mark.parametrize('mutation', ['failed', 'nonfixture', 'missing', 'duplicate', 'mock', 'unsafe_path', 'bad_hash'])
def test_unverified_roundtrip_cannot_supply_probe_targets(tmp_path, mutation):
    paths = write_inputs(tmp_path)
    run = json.loads(paths[2].read_text())
    if mutation == 'failed': run['status'] = 'failed'
    if mutation == 'nonfixture': run['real_user_account_used'] = True
    if mutation == 'missing': run['operations'].pop()
    if mutation == 'duplicate': run['operations'][1] = run['operations'][0]
    if mutation == 'mock': run['operations'][0]['remote_id'] = 'MOCK-1'
    if mutation == 'unsafe_path': run['operations'][0]['remote_id'] = '../User/Administrator'
    if mutation == 'bad_hash': run['operations'][0]['snapshot_hash'] = 'not-a-hash'
    paths[2].write_text(json.dumps(run))
    with pytest.raises(ValueError, match='ROUNDTRIP_NOT_VERIFIED'):
        probe.validate_inputs(*paths)


@pytest.mark.parametrize('file_index', [0, 1, 2])
@pytest.mark.parametrize('bad', ['oversized', 'duplicate', 'malformed'])
def test_untrusted_input_files_are_strict_and_bounded(tmp_path, file_index, bad):
    paths = write_inputs(tmp_path)
    if bad == 'oversized':
        paths[file_index].write_bytes(b'x' * (probe.MAX_INPUT_BYTES + 1))
    elif bad == 'duplicate':
        paths[file_index].write_text('PF_EPHEMERAL_NONCE=a\nPF_EPHEMERAL_NONCE=a\n' if file_index == 1
                                     else '{"status":"failed","status":"passed"}')
    else:
        paths[file_index].write_bytes(b'\xff')
    with pytest.raises((ValueError, UnicodeError)):
        probe.validate_inputs(*paths)


def test_no_explicit_opt_in_never_reads_or_networks(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'validate_inputs', lambda *_: pytest.fail('no input read without opt-in'))
    out = tmp_path / 'report.json'
    assert probe.main(['--roundtrip', str(tmp_path / 'absent'), '--output', str(out)]) == 2
    assert json.loads(out.read_text())['network_attempted'] is False


@pytest.mark.parametrize('status,code', [('passed', 0), ('failed', 1)])
def test_cli_writes_sanitized_report_and_retains_exit_status(tmp_path, monkeypatch, capsys, status, code):
    paths = write_inputs(tmp_path)
    report = {'stage': 'permission-probes', 'status': status}
    monkeypatch.setattr(probe, 'exercise', lambda *_: report)
    out = tmp_path / 'report.json'
    assert probe.main(['--ephemeral-test', '--credentials', str(paths[0]), '--env-file', str(paths[1]),
                       '--roundtrip', str(paths[2]), '--output', str(out)]) == code
    assert json.loads(out.read_text()) == report
    assert json.loads(capsys.readouterr().out) == report


def test_aggregate_contract_accepts_only_complete_exact_probe_set():
    assert probe.require_probe_evidence(passed_record())['status'] == 'passed'
    for index in range(len(probe.PROBES)):
        for field, value in [('method', 'GET'), ('endpoint', 'wrong'), ('status', 400),
                             ('error_type', 'AuthenticationError'), ('permission_denied', 1)]:
            record = passed_record()
            if record['probes'][index][field] == value and type(record['probes'][index][field]) is type(value):
                continue
            record['probes'][index][field] = value
            with pytest.raises(ValueError):
                probe.require_probe_evidence(record)
        record = passed_record()
        record['probes'].pop(index)
        with pytest.raises(ValueError):
            probe.require_probe_evidence(record)


@pytest.mark.parametrize('field', probe.REQUIRED_TRUE)
@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_aggregate_contract_rejects_missing_or_coerced_positive_controls(field, value):
    record = passed_record()
    record[field] = value
    with pytest.raises(ValueError):
        probe.require_probe_evidence(record)


def test_aggregate_contract_rejects_reordering_repeats_and_extra_response_fields():
    for mutation in ('reverse', 'duplicate', 'extra'):
        record = copy.deepcopy(passed_record())
        if mutation == 'reverse': record['probes'].reverse()
        if mutation == 'duplicate': record['probes'][0] = record['probes'][1]
        if mutation == 'extra': record['probes'][0]['response'] = CANARY
        with pytest.raises(ValueError):
            probe.require_probe_evidence(record)
