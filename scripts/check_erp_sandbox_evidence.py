"""Check all disposable ERP gate records; not an independent execution or attestation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/erpnext/sandbox'))
from probe_erp_permissions import require_probe_evidence, PROBES, REQUIRED_TRUE as PROBE_REQUIRED_TRUE
from lab_permissions import require_select_only
from cost_fixtures import COST_CASES

FILES = ('seed.json', 'roundtrip.json', 'database-audit.json', 'image-digests.json', 'permission-probes.json')
STEPS = [
    'dedicated_identity_get_only_preflight',
    'unapproved_self_approved_and_stale_writes_denied',
    'normal_independent_worker_draft_readback_and_replay',
    'lost-receipt_independent_worker_draft_readback_and_replay',
    'excluded-discount_independent_worker_draft_readback_and_replay',
    'included-discount_independent_worker_draft_readback_and_replay',
    'api_process_restart_retains_remote_ids',
]


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def count_is(value, expected: int) -> bool:
    return type(value) is int and value == expected


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def check(directory: Path, backend='sqlite') -> dict:
    records, digests = {}, {}
    for name in FILES:
        with (directory / name).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, 'OVERSIZED_EVIDENCE')
        records[name] = json.loads(raw, object_pairs_hook=unique_object)
        digests[name] = hashlib.sha256(raw).hexdigest()
    seed, run, audit, images, probes = (records[name] for name in FILES)
    require_probe_evidence(probes)
    require(backend in {'sqlite', 'postgresql'}, 'INVALID_EXPECTED_BACKEND')
    require(all(isinstance(x, dict) and x.get('status') == 'passed' for x in (seed, run, audit)), 'INCOMPLETE_STAGES')
    require(seed.get('stage') == 'seed' and seed.get('synthetic_only') is True, 'INVALID_SEED')
    require(seed.get('integration_user_type') == 'System User', 'INVALID_INTEGRATION_USER_TYPE')
    require(all(record.get('currency_precision') == '2' and record.get('float_precision') == '6' and record.get('rounding_method') == 'Commercial Rounding'
                for record in (seed, audit)), 'UNVERIFIED_COST_PRECISION')
    permissions = seed.get('permissions', {})
    require(all(permissions.get(p) is True for p in ('read', 'create', 'write')) and
            all(permissions.get(p) is False for p in ('submit', 'cancel', 'delete')), 'UNSAFE_SEED_PERMISSIONS')
    require(run.get('synthetic_only') is True and all(run.get(k) is False for k in
        ('real_user_account_used', 'human_approval_measured', 'model_used')), 'INVALID_SCOPE')
    require(run.get('steps') == STEPS, 'MISSING_OR_REPEATED_STEPS')
    require(count_is(run.get('post_attempts'), len(COST_CASES)) and count_is(run.get('real_erpnext_drafts_verified'), len(COST_CASES)), 'INVALID_WRITE_COUNTS')
    require(run.get('api_business_database') == {'sqlite': 'SQLite', 'postgresql': 'PostgreSQL'}[backend]
            and run.get('real_erp_database') == 'MariaDB', 'INVALID_DATABASE_SCOPE')
    if backend == 'postgresql':
        business = run.get('business_database_audit', {})
        require(business.get('status') == 'passed' and business.get('database') == 'PostgreSQL', 'BUSINESS_AUDIT_MISSING')
        require(all(business.get(key) is True for key in ('isolated_schema', 'migration_current',
            'operation_identity_matches', 'read_only_audit', 'after_api_restart')), 'BUSINESS_AUDIT_INCOMPLETE')
        require(all(count_is(business.get(key), len(COST_CASES)) for key in ('operation_count', 'completed_operation_count',
            'outbox_count', 'done_outbox_count', 'verified_receipt_count')), 'BUSINESS_AUDIT_INVALID_COUNTS')
        require(isinstance(business.get('server_version_num'), str)
            and re.fullmatch('[0-9]{5,6}', business['server_version_num']) is not None, 'POSTGRES_VERSION_MISSING')
    preflight = run.get('preflight', {})
    require(preflight.get('integration_identity_verified') is True and preflight.get('company_currency_verified') is True
            and preflight.get('write_probe_performed') is False,
            'IDENTITY_NOT_VERIFIED')
    operations = run.get('operations')
    require(isinstance(operations, list) and len(operations) == len(COST_CASES) and all(isinstance(x, dict) for x in operations),
            'INVALID_OPERATIONS')
    require([x.get('scenario') for x in operations] == list(COST_CASES), 'INVALID_SCENARIOS')
    for op in operations:
        require(isinstance(op.get('operation_id'), str) and bool(op['operation_id']), 'INVALID_OPERATION_ID')
        require(isinstance(op.get('remote_id'), str) and bool(op['remote_id']) and not op['remote_id'].startswith('MOCK-'),
                'INVALID_REMOTE_ID')
        require(isinstance(op.get('snapshot_hash'), str) and re.fullmatch('[0-9a-f]{64}', op['snapshot_hash']) is not None,
                'INVALID_SNAPSHOT_HASH')
        require(op.get('expected_total') == COST_CASES[op['scenario']]['total'] and op.get('cost_components_verified') is True, 'INVALID_EXPECTED_TOTAL')
    require(len({x['operation_id'] for x in operations}) == len(COST_CASES) and len({x['remote_id'] for x in operations}) == len(COST_CASES),
            'DUPLICATE_OPERATION_OR_DRAFT')
    require(audit.get('stage') == 'database-audit' and count_is(audit.get('draft_count'), len(COST_CASES)) and
            count_is(audit.get('purchase_order_count'), 0) and count_is(audit.get('submitted_count'), 0), 'INVALID_DATABASE_COUNTS')
    require_select_only(seed.get('reference_permissions'))
    require_select_only(audit.get('reference_permissions'))
    for record in (seed, audit):
        refs = record.get('cost_reference_permissions')
        require(isinstance(refs, dict) and set(refs) == {'tax', 'freight'}, 'COST_REFERENCE_PERMISSIONS_MISSING')
        for permissions in refs.values():
            require_select_only(permissions)
    require(all(audit.get(k) is True for k in ('remote_unique_index_present',
        'duplicate_key_update_rejected_and_rolled_back', 'adapter_readback_independently_checked', 'cost_components_independently_checked')), 'DATABASE_CHECKS_MISSING')
    require(all(audit.get('restricted_permissions', {}).get(k) is True for k in ('submit', 'cancel', 'delete')),
            'UNSAFE_AUDIT_PERMISSIONS')
    require(all(isinstance(audit.get(k), str) and bool(audit[k]) for k in
        ('erpnext_version', 'frappe_version', 'database_version')), 'VERSIONS_MISSING')
    require(isinstance(images, list) and len(images) > 0 and all(isinstance(x, str) and
        re.fullmatch(r'frappe/erpnext@sha256:[0-9a-f]{64}', x) for x in images), 'IMAGE_DIGEST_MISSING')
    return {'status': 'passed', 'record_sha256': digests, 'synthetic_only': True,
        'normal_and_lost_receipt_checked': True, 'database_audit_checked': True,
        'api_business_database': run['api_business_database'], 'negative_rest_permissions_checked': True,
        'business_database_audit_checked': backend == 'postgresql',
        'scope': 'Consistency check of CI records, not a new ERP execution or cryptographic attestation'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--business-database', choices=('sqlite', 'postgresql'), default='sqlite')
    args = parser.parse_args(argv)
    try:
        result = check(args.directory, args.business_database)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Never echo malformed record content or paths into public CI output.
        print(json.dumps({'status': 'failed', 'reason': 'ERP_EVIDENCE_INCOMPLETE_OR_INVALID'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
