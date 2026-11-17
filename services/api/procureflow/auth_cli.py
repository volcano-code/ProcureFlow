"""Offline controlled pilot administration; never exposed as an HTTP endpoint.

Commands read a single bounded JSON object from stdin. Every mutation requires
--apply. Invitation plaintext is written once to a new owner-only file selected
with --credential-output, never to arguments, stdout, logs, audit, or the DB.
The operator must arrange authorized delivery and delete the plaintext after use.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .auth import IdentityService
from .config import Settings
from .db import Database
from .errors import DomainError

COMMANDS = {
    "create-tenant": ("create_tenant", {"tenant_id"}, set()),
    "set-membership": ("set_membership", {"tenant_id", "user_id", "role"}, {"active", "expires_at"}),
    "set-tenant-active": ("set_tenant_active", {"tenant_id", "active"}, set()),
    "issue-invite": ("issue_invite", {"tenant_id", "user_id"}, {"ttl_seconds"}),
    "revoke-invite": ("revoke_invite", {"invite_id"}, set()),
    "revoke-session": ("revoke_session", {"session_id"}, set()),
    "revoke-user-sessions": ("revoke_user_sessions", {"tenant_id", "user_id"}, set()),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--apply", action="store_true", help="explicitly authorize this local durable mutation")
    parser.add_argument("--credential-output", type=Path, help="new owner-only invitation file; issue-invite only")
    args = parser.parse_args(argv)
    if not args.apply:
        print("Mutation not performed: --apply is required", file=sys.stderr)
        return 2
    if bool(args.credential_output) != (args.command == "issue-invite"):
        print("issue-invite requires --credential-output; other commands forbid it", file=sys.stderr)
        return 2
    db, descriptor = None, None
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError("INPUT_LIMIT")
        body = json.loads(raw)
        method, required, optional = COMMANDS[args.command]
        if not isinstance(body, dict) or not required <= body.keys() or not body.keys() <= required | optional:
            raise ValueError("INVALID_COMMAND_FIELDS")
        settings = Settings()
        if settings.mode != "pilot":
            raise ValueError("PILOT_MODE_REQUIRED")
        db = Database(settings.database_url)
        db.check_ready()  # CLI never creates or migrates schemas implicitly.
        if args.credential_output:
            # Reserve before issuance so a pre-existing file cannot consume a
            # credential; O_EXCL also rejects symlinks and accidental overwrite.
            descriptor = os.open(args.credential_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        result = getattr(IdentityService(db, settings), method)(**body)
        if descriptor is not None:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = None
                output.write(result.pop("credential") + "\n")
                output.flush()
                os.fsync(output.fileno())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # Never include exception, body, settings or SQL details in operator logs.
        # A failed plaintext delivery can leave an issued but unclaimed invite;
        # the operator should revoke it before issuing another.
        if descriptor is not None:
            os.close(descriptor)
        code = error.code if isinstance(error, DomainError) else "PILOT_ADMIN_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
