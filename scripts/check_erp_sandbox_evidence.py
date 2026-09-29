"""Check all disposable ERP gate records; not an independent execution or attestation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

FILES = ('seed.json', 'roundtrip.json', 'database-audit.json', 'image-digests.json')
STEPS = [
    'dedicated_identity_get_only_preflight',
    'unapproved_self_approved_and_stale_writes_denied',
    'normal_independent_worker_draft_readback_and_replay',
    'lost-receipt_independent_worker_draft_readback_and_replay',
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


def check(directory: Path) -> dict:
    records, digests = {}, {}
    for name in FILES:
        with (directory / name).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, 'OVERSIZED_EVIDENCE')
        records[name] = json.loads(raw, object_pairs_hook=unique_object)
        digests[name] = hashlib.sha256(raw).hexdigest()
    seed, run, audit, images = (records[name] for name in FILES)
    require(all(isinstance(x, dict) and x.get('status') == 'passed' for x in (seed, run, audit)), 'INCOMPLETE_STAGES')
    require(seed.get('stage') == 'seed' and seed.get('synthetic_only') is True, 'INVALID_SEED')
    require(seed.get('integration_user_type') == 'System User', 'INVALID_INTEGRATION_USER_TYPE')
    permissions = seed.get('permissions', {})
    require(all(permissions.get(p) is True for p in ('read', 'create', 'write')) and
            all(permissions.get(p) is False for p in ('submit', 'cancel', 'delete')), 'UNSAFE_SEED_PERMISSIONS')
    require(run.get('synthetic_only') is True and all(run.get(k) is False for k in
        ('real_user_account_used', 'human_approval_measured', 'model_used')), 'INVALID_SCOPE')
    require(run.get('steps') == STEPS, 'MISSING_OR_REPEATED_STEPS')
    require(count_is(run.get('post_attempts'), 2) and count_is(run.get('real_erpnext_drafts_verified'), 2), 'INVALID_WRITE_COUNTS')
    require(run.get('api_business_database') == 'SQLite' and run.get('real_erp_database') == 'MariaDB', 'INVALID_DATABASE_SCOPE')
    preflight = run.get('preflight', {})
    require(preflight.get('integration_identity_verified') is True and preflight.get('write_probe_performed') is False,
            'IDENTITY_NOT_VERIFIED')
    operations = run.get('operations')
    require(isinstance(operations, list) and len(operations) == 2 and all(isinstance(x, dict) for x in operations),
            'INVALID_OPERATIONS')
    require([x.get('scenario') for x in operations] == ['normal', 'lost-receipt'], 'INVALID_SCENARIOS')
    for op in operations:
        require(isinstance(op.get('operation_id'), str) and bool(op['operation_id']), 'INVALID_OPERATION_ID')
        require(isinstance(op.get('remote_id'), str) and bool(op['remote_id']) and not op['remote_id'].startswith('MOCK-'),
                'INVALID_REMOTE_ID')
        require(isinstance(op.get('snapshot_hash'), str) and re.fullmatch('[0-9a-f]{64}', op['snapshot_hash']) is not None,
                'INVALID_SNAPSHOT_HASH')
        require(op.get('expected_total') == '2000.00', 'INVALID_EXPECTED_TOTAL')
    require(len({x['operation_id'] for x in operations}) == 2 and len({x['remote_id'] for x in operations}) == 2,
            'DUPLICATE_OPERATION_OR_DRAFT')
    require(audit.get('stage') == 'database-audit' and count_is(audit.get('draft_count'), 2) and
            count_is(audit.get('purchase_order_count'), 0) and count_is(audit.get('submitted_count'), 0), 'INVALID_DATABASE_COUNTS')
    require(all(audit.get(k) is True for k in ('remote_unique_index_present',
        'duplicate_key_update_rejected_and_rolled_back', 'adapter_readback_independently_checked')), 'DATABASE_CHECKS_MISSING')
    require(all(audit.get('restricted_permissions', {}).get(k) is True for k in ('submit', 'cancel', 'delete')),
            'UNSAFE_AUDIT_PERMISSIONS')
    require(all(isinstance(audit.get(k), str) and bool(audit[k]) for k in
        ('erpnext_version', 'frappe_version', 'database_version')), 'VERSIONS_MISSING')
    require(isinstance(images, list) and len(images) > 0 and all(isinstance(x, str) and
        re.fullmatch(r'frappe/erpnext@sha256:[0-9a-f]{64}', x) for x in images), 'IMAGE_DIGEST_MISSING')
    return {'status': 'passed', 'record_sha256': digests, 'synthetic_only': True,
        'normal_and_lost_receipt_checked': True, 'database_audit_checked': True,
        'scope': 'Consistency check of CI records, not a new ERP execution or cryptographic attestation'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.directory)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Never echo malformed record content or paths into public CI output.
        print(json.dumps({'status': 'failed', 'reason': 'ERP_EVIDENCE_INCOMPLETE_OR_INVALID'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
