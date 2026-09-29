"""Select-only Account dependency and evidence gate for the disposable ERP lab.

No Frappe import, credentials, networking, or permission mutation here. The seed
alone provisions permissions inside its guarded lab_session. Both the seed and
independent audit query effective permissions on the actual payable account.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
from typing import Any

ACCOUNT_RIGHTS = (
    'select', 'read', 'create', 'write', 'delete', 'submit', 'cancel',
    'amend', 'import', 'export', 'report', 'print', 'email', 'share',
)
MAX_RECORD_BYTES = 65536


def require_select_only(permissions: Any) -> dict[str, bool]:
    """Reject missing, extra, non-boolean, denied-select or escalated rights."""
    if (not isinstance(permissions, dict) or set(permissions) != set(ACCOUNT_RIGHTS)
            or any(type(value) is not bool for value in permissions.values())
            or permissions['select'] is not True
            or any(permissions[right] for right in ACCOUNT_RIGHTS if right != 'select')):
        raise ValueError('ACCOUNT_REFERENCE_NOT_SELECT_ONLY')
    return dict(permissions)


def verify_account_reference(
    has_permission: Callable[..., Any], user: str, account: str,
) -> dict[str, bool]:
    """Read real effective permissions, never repair them or use Administrator."""
    if user != 'pf-integration@example.invalid' or not isinstance(account, str) or not account.strip():
        raise ValueError('ACCOUNT_REFERENCE_CONTEXT_INVALID')
    observed = {
        right: bool(has_permission('Account', right, doc=account, user=user))
        for right in ACCOUNT_RIGHTS
    }
    return require_select_only(observed)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def check_records(directory: Path) -> dict:
    digests = {}
    for filename, stage in (('seed.json', 'seed'), ('database-audit.json', 'database-audit')):
        with (directory / filename).open('rb') as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError('OVERSIZED_REFERENCE_EVIDENCE')
        record = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(record, dict) or record.get('status') != 'passed' or record.get('stage') != stage:
            raise ValueError('REFERENCE_STAGE_NOT_PASSED')
        require_select_only(record.get('reference_permissions'))
        digests[filename] = hashlib.sha256(raw).hexdigest()
    return {'status': 'passed', 'account_select_only': True, 'record_sha256': digests,
            'scope': 'Seed and post-roundtrip Account permission records; not a general security certification'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = check_records(args.directory)
    except (OSError, ValueError, TypeError, KeyError):
        # Do not echo malformed records, paths, account names, or exception text.
        print(json.dumps({'status': 'failed', 'reason': 'REFERENCE_PERMISSION_EVIDENCE_INVALID'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
