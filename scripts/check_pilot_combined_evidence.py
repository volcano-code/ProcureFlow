"""Strict consistency check for new pilot CI records, never a new ERP execution."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path[:0] = [str(Path(__file__).resolve().parent),
               str(Path(__file__).resolve().parents[1] / 'integrations/erpnext/sandbox')]
from pilot_gate_contract import SCENARIOS, DENIALS, BROWSER_CHECKS, STEPS
from check_erp_sandbox_evidence import require_tabular_provenance
from probe_erp_permissions import load_json
from lab_permissions import require_select_only
from cost_fixtures import ACCEPTANCE_CASES

FILES = ('seed.json', 'roundtrip.json', 'database-audit.json', 'image-digests.json')


def require(condition):
    if not condition: raise ValueError('PILOT_EVIDENCE_INCOMPLETE_OR_INVALID')


def count(value, expected):
    return type(value) is int and value == expected


def check(directory, backend='sqlite'):
    records = {name: load_json(directory / name) for name in FILES}
    seed, run, audit, images = [records[name] for name in FILES]
    require(all(isinstance(record, dict) and record.get('status') == 'passed' for record in (seed, run, audit)))
    require(seed.get('stage') == 'seed' and seed.get('synthetic_only') is True)
    require(run.get('synthetic_only') is True and run.get('fixture') is False and run.get('live_erp') is True
        and run.get('model_used') is False and run.get('human_approval_measured') is False
        and run.get('real_user_account_used') is False and run.get('browser_verified') is True)
    require(run.get('api_business_database') == backend and run.get('real_erp_database') == 'MariaDB')
    require(run.get('steps') == list(STEPS))
    require(count(run.get('post_attempts'), 2) and count(run.get('real_erpnext_drafts_verified'), 2)
        and count(run.get('mock_drafts_verified'), 0))
    preflight = run.get('preflight', {})
    require(preflight.get('integration_identity_verified') is True and preflight.get('company_currency_verified') is True
        and preflight.get('write_probe_performed') is False)
    operations, denials = run.get('operations'), run.get('denials')
    require(isinstance(operations, list) and len(operations) == 2 and all(isinstance(op, dict) for op in operations))
    require(isinstance(denials, list) and len(denials) == 3 and all(isinstance(op, dict) for op in denials))
    require([op.get('scenario') for op in operations] == list(SCENARIOS))
    require([op.get('scenario') for op in denials] == list(DENIALS))
    source_hashes = {}
    for op in operations + denials:
        require(isinstance(op.get('operation_id'), str) and re.fullmatch('[0-9a-f]{64}', op['operation_id']) is not None)
        require(isinstance(op.get('snapshot_hash'), str) and re.fullmatch('[0-9a-f]{64}', op['snapshot_hash']) is not None)
        require(isinstance(op.get('browser_checks'), dict)
            and all(op['browser_checks'].get(flag) is True for flag in BROWSER_CHECKS))
    require(len({op['operation_id'] for op in operations + denials}) == 5)
    for op in operations:
        case = ACCEPTANCE_CASES[op['scenario']]
        require(op.get('final_status') == 'COMPLETED' and isinstance(op.get('remote_id'), str)
            and bool(op['remote_id']) and not op['remote_id'].startswith('MOCK-'))
        require(op.get('expected_total') == case['total'] and op.get('input_format') == case['input_format'])
        require(all(op.get(key) is True for key in ('worker_process_independent', 'cost_components_verified',
            'tabular_provenance_verified', 'readback_verified', 'stale_session_denied', 'stale_session_mutation_denied')))
        require(count(op.get('post_attempts'), 1))
        lost = case['lose_receipt']
        require(all(op.get(key) is lost for key in ('logout_before_recovery', 'recovery_read_only', 'api_restarted_before_recovery')))
        require(all(op.get(key) is (not lost) for key in ('session_expired_after_enqueue', 'accepted_work_survives_session_expiry')))
        require_tabular_provenance(op)
        path = f"input-fixtures/synthetic-{op['scenario']}.{case['input_format']}"
        with (directory / path).open('rb') as source: content = source.read(2 * 1024 * 1024 + 1)
        require(0 < len(content) <= 2 * 1024 * 1024)
        source_hashes[path] = hashlib.sha256(content).hexdigest()
        require(source_hashes[path] == op['provenance']['document_sha256'])
    require(len({op['remote_id'] for op in operations}) == 2)
    require(all(len({op['provenance'][key] for op in operations}) == 2 for key in
        ('document_sha256', 'document_id', 'import_id', 'quote_id')))
    for op in denials:
        require(op.get('final_status') == 'NEEDS_HUMAN' and op.get('remote_id') is None)
        require(op.get('denial_reason') == ('BUYER_REVOKED' if op['scenario'] == 'buyer-revocation' else 'APPROVER_REVOKED'))
        require(count(op.get('stale_session_http_status'), 401) and count(op.get('fresh_workers'), 2)
            and count(op.get('first_write_attempts'), 0) and op.get('api_restarted') is True)
    business = run.get('business_database_audit', {})
    require(business.get('status') == 'passed' and business.get('database') == backend)
    require(all(business.get(key) is True for key in ('read_only_audit', 'after_api_restart',
        'migration_current', 'operation_identity_matches', 'durable_pilot_identity_rows', 'buyer_approver_binding_checked')))
    require(all(count(business.get(key), expected) for key, expected in
        [('operation_count', 5), ('completed_operation_count', 2), ('denied_operation_count', 3), ('done_outbox_count', 5), ('verified_receipt_count', 2), ('denied_dispatch_attempts', 0)]))
    if backend == 'postgresql':
        require(business.get('isolated_schema') is True and isinstance(business.get('server_version_num'), str)
            and re.fullmatch('[0-9]{5,6}', business['server_version_num']) is not None)
    require(audit.get('stage') == 'database-audit' and audit.get('scope') == 'pilot-native-two-draft')
    require(all(count(audit.get(key), expected) for key, expected in [('draft_count', 2),
        ('purchase_order_count', 0), ('submitted_count', 0), ('denied_operation_draft_count', 0), ('operation_count_checked', 5)]))
    require(all(audit.get(key) is True for key in ('read_only_audit', 'remote_unique_index_present',
        'adapter_readback_independently_checked', 'cost_components_independently_checked', 'purchase_order_create_denied')))
    require(all(audit.get('restricted_permissions', {}).get(key) is True for key in ('submit', 'cancel', 'delete')))
    for record in (seed, audit):
        require_select_only(record.get('reference_permissions'))
        require(set(record.get('cost_reference_permissions', {})) == {'tax', 'freight'})
        for permissions in record['cost_reference_permissions'].values(): require_select_only(permissions)
    require(all(isinstance(audit.get(key), str) and audit[key] for key in ('erpnext_version', 'frappe_version', 'database_version')))
    require(isinstance(images, list) and images and all(isinstance(value, str) and
        re.fullmatch('frappe/erpnext@sha256:[0-9a-f]{64}', value) for value in images))
    return {'status': 'passed', 'synthetic_only': True, 'api_business_database': backend,
        'scope': 'Consistency check of pilot CI records, not a new ERP execution or attestation',
        'record_sha256': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES},
        'source_sha256': source_hashes, 'native_pilot_browser_checked': True,
        'independent_database_audit_checked': True, 'membership_first_write_denial_checked': True,
        'session_expiry_preserves_accepted_work_checked': True, 'logout_recovery_read_only_checked': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--business-database', choices=('sqlite', 'postgresql'), default='sqlite')
    args = parser.parse_args(argv)
    try: result = check(args.directory, args.business_database)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print(json.dumps({'status': 'failed', 'reason': 'PILOT_EVIDENCE_INCOMPLETE_OR_INVALID'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
