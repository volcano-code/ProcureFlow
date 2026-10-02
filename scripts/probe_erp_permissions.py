"""Negative REST authorization probes for this repository's disposable ERP lab.

Never point this script at a user account or production ERP. The destination is
fixed to the loopback sandbox; explicit opt-in, matching seed markers, a passed
roundtrip, and positive fixture/identity reads are required before any mutation
attempt. If a denied action unexpectedly succeeds, stop and fail: do not repair
permissions or hide the change. The subsequent independent database audit must
also pass. Response bodies, identifiers, and credentials never enter evidence.
"""
from __future__ import annotations

import argparse
import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from urllib.parse import quote

import httpx
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/erpnext/sandbox'))
from cost_fixtures import COST_CASES

TARGET = 'http://127.0.0.1:18080'
MAX_INPUT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
PROBES = (
    ('account_document_read', 'GET', 'Account'),
    ('account_document_write', 'PUT', 'Account'),
    ('account_document_delete', 'DELETE', 'Account'),
    ('draft_submit', 'PUT', 'Supplier Quotation'),
    ('draft_delete', 'DELETE', 'Supplier Quotation'),
    ('purchase_order_list_read', 'GET', 'Purchase Order'),
    ('purchase_order_create', 'POST', 'Purchase Order'),
)
REQUIRED_TRUE = (
    'synthetic_only', 'network_attempted', 'integration_identity_verified',
    'lab_fixture_verified', 'account_select_verified', 'draft_fixtures_verified',
    'drafts_unchanged',
)
SAFE_ERRORS = frozenset({
    'IDENTITY_NOT_VERIFIED', 'LAB_FIXTURE_NOT_VERIFIED', 'ACCOUNT_SELECT_NOT_VERIFIED',
    'DRAFT_FIXTURE_NOT_VERIFIED', 'DRAFTS_CHANGED', 'EXPECTED_PERMISSION_DENIAL_NOT_OBSERVED',
    'REMOTE_RESPONSE_INVALID', 'REMOTE_RESPONSE_OVERSIZED', 'ERP_TRANSPORT_FAILED',
})


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError('NONFINITE_JSON_VALUE')


def read_bounded(path):
    with Path(path).open('rb') as stream:
        value = stream.read(MAX_INPUT_BYTES + 1)
    require(len(value) <= MAX_INPUT_BYTES, 'OVERSIZED_INPUT')
    return value


def load_json(path):
    return json.loads(read_bounded(path), object_pairs_hook=unique_object, parse_constant=invalid_constant)


def valid_identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 140 and value.strip() == value
            and value not in {'.', '..'} and not any(ord(c) < 32 or c in '/\\?#' for c in value))


def validate_inputs(credentials, env_file, roundtrip):
    """Fail before networking on malformed, nonfixture, or incomplete inputs."""
    lines = read_bounded(env_file).decode('utf-8').splitlines()
    marker = unique_object(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))
    data, run = load_json(credentials), load_json(roundtrip)
    require(isinstance(data, dict) and isinstance(run, dict), 'LAB_CONFIGURATION_MISMATCH')
    expected = {'site': 'pf-erp-test.local', 'company': 'ProcureFlow Sandbox',
                'user': 'pf-integration@example.invalid', 'sku': 'PF-SANDBOX-ITEM'}
    require(all(data.get(key) == value for key, value in expected.items()), 'LAB_CONFIGURATION_MISMATCH')
    nonce = data.get('nonce')
    require(isinstance(nonce, str) and re.fullmatch(r'[0-9a-f]{64}', nonce) is not None
            and nonce == marker.get('PF_EPHEMERAL_NONCE'), 'LAB_CONFIGURATION_MISMATCH')
    require(all(isinstance(data.get(k), str) and 0 < len(data[k]) <= 256
                and not any(c.isspace() for c in data[k]) for k in ('api_key', 'api_secret')),
            'LAB_CONFIGURATION_MISMATCH')
    require(valid_identifier(data.get('supplier')), 'LAB_CONFIGURATION_MISMATCH')
    require(run.get('status') == 'passed' and run.get('synthetic_only') is True
            and run.get('real_user_account_used') is False, 'ROUNDTRIP_NOT_VERIFIED')
    operations = run.get('operations')
    require(isinstance(operations, list) and len(operations) == len(COST_CASES), 'ROUNDTRIP_NOT_VERIFIED')
    require([op.get('scenario') if isinstance(op, dict) else None for op in operations] == list(COST_CASES),
            'ROUNDTRIP_NOT_VERIFIED')
    for operation in operations:
        require(isinstance(operation, dict) and valid_identifier(operation.get('remote_id'))
                and not operation['remote_id'].startswith('MOCK-')
                and valid_identifier(operation.get('operation_id'))
                and isinstance(operation.get('snapshot_hash'), str)
                and re.fullmatch(r'[0-9a-f]{64}', operation['snapshot_hash']) is not None
                and operation.get('scenario') in COST_CASES
                and operation.get('expected_total') == COST_CASES[operation['scenario']]['total'], 'ROUNDTRIP_NOT_VERIFIED')
    require(len({op['remote_id'] for op in operations}) == len(COST_CASES)
            and len({op['operation_id'] for op in operations}) == len(COST_CASES), 'ROUNDTRIP_NOT_VERIFIED')
    return data, operations


def resource(doctype, name=None):
    path = '/api/resource/' + quote(doctype, safe='')
    return path if name is None else path + '/' + quote(name, safe='')


def response_json(response):
    require(len(response.content) <= MAX_RESPONSE_BYTES, 'REMOTE_RESPONSE_OVERSIZED')
    try:
        value = json.loads(response.content, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError):
        raise ValueError('REMOTE_RESPONSE_INVALID') from None
    require(isinstance(value, dict), 'REMOTE_RESPONSE_INVALID')
    return value


def denial_observation(probe, response):
    """A 4xx, missing record, bad payload, or HTML 403 is NOT a permission pass."""
    name, method, endpoint = probe
    observed = {'name': name, 'method': method, 'endpoint': endpoint,
                'status': response.status_code, 'permission_denied': False}
    try:
        body = response_json(response)
    except ValueError:
        return observed
    kind = body.get('exc_type')
    if isinstance(kind, str) and kind in {'PermissionError', 'AuthenticationError', 'ValidationError', 'MandatoryError',
                'LinkValidationError', 'DoesNotExistError', 'DocstatusTransitionError'}:
        observed['error_type'] = kind
    observed['permission_denied'] = response.status_code == 403 and kind == 'PermissionError'
    return observed


def require_probe_evidence(record):
    """Shared fail-closed contract for the aggregate evidence checker."""
    require(isinstance(record, dict) and record.get('stage') == 'permission-probes'
            and record.get('status') == 'passed', 'PERMISSION_PROBES_NOT_PASSED')
    require(all(record.get(key) is True for key in REQUIRED_TRUE)
            and record.get('real_user_account_used') is False
            and type(record.get('draft_documents_checked')) is int
            and record['draft_documents_checked'] == len(COST_CASES), 'PERMISSION_PROBE_CONTEXT_INVALID')
    observed = record.get('probes')
    require(isinstance(observed, list) and len(observed) == len(PROBES), 'PERMISSION_PROBES_INCOMPLETE')
    for row, (name, method, endpoint) in zip(observed, PROBES, strict=True):
        require(isinstance(row, dict) and row == {
            'name': name, 'method': method, 'endpoint': endpoint, 'status': 403,
            'error_type': 'PermissionError', 'permission_denied': True,
        } and type(row['status']) is int and row['permission_denied'] is True,
            'PERMISSION_PROBE_NOT_DENIED')
    return record


def draft_snapshot(client, data, operation):
    response = client.get(resource('Supplier Quotation', operation['remote_id']))
    require(response.status_code == 200, 'DRAFT_FIXTURE_NOT_VERIFIED')
    doc = response_json(response).get('data')
    require(isinstance(doc, dict), 'DRAFT_FIXTURE_NOT_VERIFIED')
    expected = {'doctype': 'Supplier Quotation', 'name': operation['remote_id'],
        'company': data['company'], 'supplier': data['supplier'], 'currency': 'CNY',
        'custom_procureflow_operation_key': operation['operation_id'],
        'custom_procureflow_snapshot_hash': operation['snapshot_hash']}
    require(all(doc.get(k) == v for k, v in expected.items()) and type(doc.get('docstatus')) is int
            and doc['docstatus'] == 0, 'DRAFT_FIXTURE_NOT_VERIFIED')
    items = doc.get('items')
    require(isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict),
            'DRAFT_FIXTURE_NOT_VERIFIED')
    item = items[0]
    try:
        require(item.get('item_code') == data['sku'] and item.get('uom') == 'EA'
                and Decimal(str(item.get('qty'))) == Decimal('20')
                and Decimal(str(item.get('rate'))) == Decimal(COST_CASES[operation['scenario']]['unit_price'])
                and Decimal(str(doc.get('grand_total'))) == Decimal(operation['expected_total']), 'DRAFT_FIXTURE_NOT_VERIFIED')
        datetime.date.fromisoformat(doc['transaction_date'])
    except (ValueError, InvalidOperation, TypeError, KeyError):
        raise ValueError('DRAFT_FIXTURE_NOT_VERIFIED') from None
    # Only compare internally. No document payload or fingerprint is exported.
    fields = (*expected, 'docstatus', 'transaction_date', 'grand_total', 'modified', 'items',
              'taxes', 'discount_amount', 'apply_discount_on', 'net_total', 'total_taxes_and_charges')
    return {field: doc.get(field) for field in fields}


def exercise(data, operations, *, transport=None):
    report = {'stage': 'permission-probes', 'status': 'running',
        'scope': 'Disposable ERPNext REST negative authorization probes; no general security certification',
        'synthetic_only': True, 'real_user_account_used': False, 'network_attempted': False, 'probes': []}
    phase = 'identity'
    try:
        # No destination override, ambient proxy, redirects, retries, or admin token.
        with httpx.Client(base_url=TARGET, timeout=30, trust_env=False, follow_redirects=False,
                headers={'Authorization': f"token {data['api_key']}:{data['api_secret']}",
                         'Accept': 'application/json'}, transport=transport) as client:
            report['network_attempted'] = True
            response = client.get('/api/method/frappe.auth.get_logged_user')
            require(response.status_code == 200 and response_json(response).get('message') == data['user'],
                    'IDENTITY_NOT_VERIFIED')
            report['integration_identity_verified'] = True
            phase = 'fixture-read'
            response = client.get(resource('Company', data['company']))
            require(response.status_code == 200, 'LAB_FIXTURE_NOT_VERIFIED')
            company = response_json(response).get('data')
            require(isinstance(company, dict) and company.get('name') == data['company']
                    and company.get('company_name') == data['company']
                    and company.get('abbr') == 'PFL'
                    and valid_identifier(company.get('default_payable_account')), 'LAB_FIXTURE_NOT_VERIFIED')
            account = company['default_payable_account']
            report['lab_fixture_verified'] = True
            # Frappe permits list selection with select-only access; demanding a
            # denial here would incorrectly reject the deliberately narrow grant.
            response = client.get(resource('Account'), params={'fields': json.dumps(['name']),
                'filters': json.dumps({'name': account}), 'limit_page_length': 2})
            require(response.status_code == 200 and response_json(response).get('data') == [{'name': account}],
                    'ACCOUNT_SELECT_NOT_VERIFIED')
            report['account_select_verified'] = True
            before = [draft_snapshot(client, data, op) for op in operations]
            report.update(draft_fixtures_verified=True, draft_documents_checked=len(before))
            account_path = resource('Account', account)
            draft_path = resource('Supplier Quotation', operations[0]['remote_id'])
            # Use genuine fixture values and a valid new PO draft shape, not an
            # empty/malformed request whose validation error could mask a grant.
            date = before[0]['transaction_date']
            purchase_order = {'doctype': 'Purchase Order', 'docstatus': 0, 'company': data['company'],
                'supplier': data['supplier'], 'currency': 'CNY', 'transaction_date': date, 'schedule_date': date,
                'items': [{'item_code': data['sku'], 'uom': 'EA', 'qty': 20, 'rate': 100, 'schedule_date': date}]}
            requests = (
                (account_path, {}),
                (account_path, {'json': {'disabled': 0}}),
                (account_path, {}),
                (draft_path, {'json': {'docstatus': 1}}),
                (draft_path, {}),
                (resource('Purchase Order'), {'params': {'fields': json.dumps(['name']), 'limit_page_length': 1}}),
                (resource('Purchase Order'), {'json': purchase_order}),
            )
            for probe, (path, kwargs) in zip(PROBES, requests, strict=True):
                phase = probe[0]
                response = client.request(probe[1], path, **kwargs)
                observed = denial_observation(probe, response)
                report['probes'].append(observed)
                require(observed['permission_denied'] is True, 'EXPECTED_PERMISSION_DENIAL_NOT_OBSERVED')
            phase = 'post-probe-readback'
            after = [draft_snapshot(client, data, op) for op in operations]
            require(before == after, 'DRAFTS_CHANGED')
            report.update(status='passed', drafts_unchanged=True)
            require_probe_evidence(report)
    except Exception as error:
        reason = 'ERP_TRANSPORT_FAILED' if isinstance(error, httpx.HTTPError) else str(error)
        report.update(status='failed', phase=phase,
                      reason=reason if reason in SAFE_ERRORS else 'PERMISSION_PROBE_FAILED')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ephemeral-test', action='store_true')
    parser.add_argument('--credentials', type=Path, default=Path('.data/erp-sandbox.json'))
    parser.add_argument('--env-file', type=Path, default=Path('.env.erp-sandbox'))
    parser.add_argument('--roundtrip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = {'stage': 'permission-probes', 'status': 'blocked', 'network_attempted': False,
              'reason': 'EXPLICIT_EPHEMERAL_TEST_REQUIRED'}
    code = 2
    if args.ephemeral_test:
        try:
            data, operations = validate_inputs(args.credentials, args.env_file, args.roundtrip)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            report['reason'] = 'LAB_OR_ROUNDTRIP_CONFIGURATION_INVALID'
        else:
            report = exercise(data, operations)
            code = 0 if report['status'] == 'passed' else 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
