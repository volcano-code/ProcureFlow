"""Offline pause, read-only recovery inventory and explicit reviewed resume.

This operator CLI never contacts ERP. A restore's held operations are never
released by resume; only newly authorized pilot work can proceed afterward.
"""
from __future__ import annotations
import argparse
import json
import sys

from .config import Settings
from .db import Database
from .errors import DomainError
from .maintenance import pause_writes, recovery_report, resume_writes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pause", "report", "resume"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--ledger-sha256")
    parser.add_argument("--restore-id")
    parser.add_argument("--acknowledge-reconciliation", action="store_true")
    parser.add_argument("--acknowledge-credentials", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "report" and not args.apply:
        print(json.dumps({"error": "EXPLICIT_APPLY_REQUIRED"}), file=sys.stderr)
        return 2
    db = None
    try:
        db = Database(Settings().database_url)
        db.check_ready()
        if args.command == "pause":
            result = pause_writes(db)
        elif args.command == "report":
            result = recovery_report(db)
        else:
            result = resume_writes(db, generation=args.generation, ledger_sha256=args.ledger_sha256,
                restore_id=args.restore_id, acknowledge_reconciliation=args.acknowledge_reconciliation,
                acknowledge_credentials=args.acknowledge_credentials)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": error.code if isinstance(error, DomainError) else "MAINTENANCE_FAILED"}), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
