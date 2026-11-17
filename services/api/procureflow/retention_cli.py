"""Offline reversible table-preview archive. No purge or filesystem deletion.

plan is a read-only dry run; apply requires a reviewed JSON plan, explicit tenant,
operator attribution and --apply. Configuration comes from PF_* environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .config import Settings
from .db import Database
from .errors import DomainError
from .retention import DEFAULT_RETENTION_HOURS, RetentionService

MAX_PLAN_BYTES = 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="dry run; no rows or source files are changed")
    plan.add_argument("--tenant", required=True)
    plan.add_argument("--operation", choices=("archive", "unarchive"), default="archive")
    plan.add_argument("--retention-hours", type=int, default=DEFAULT_RETENTION_HOURS,
                      help="hours after preview expiry before archiving; default 24, range 1..8760")
    plan.add_argument("--limit", type=int, default=100, help="maximum entries, range 1..1000")
    plan.add_argument("--import-id", action="append", dest="import_ids", help="required for explicit unarchive")
    plan.add_argument("--output", type=Path, help="new owner-only plan file; otherwise prints JSON")
    apply = commands.add_parser("apply", help="apply or safely replay a reviewed plan")
    apply.add_argument("--tenant", required=True)
    apply.add_argument("--actor", required=True, help="authorized operator identifier for the audit trail")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "apply" and not args.apply:
        print("Mutation not performed: --apply is required", file=sys.stderr)
        return 2
    db = None
    try:
        body = None
        if args.command == "apply":
            with args.plan.open("rb") as source:
                raw = source.read(MAX_PLAN_BYTES + 1)
            if len(raw) > MAX_PLAN_BYTES:
                raise DomainError("RETENTION_PLAN_INVALID", "Plan exceeds size limit", 422)
            body = json.loads(raw)
        settings = Settings()
        db = Database(settings.database_url)
        db.check_ready(require_migrations=not (db.sqlite and settings.mode == "demo"))
        service = RetentionService(db)
        if args.command == "plan":
            result = service.plan(args.tenant, operation=args.operation, retention_hours=args.retention_hours,
                                  limit=args.limit, import_ids=args.import_ids)
            if args.output:
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                    json.dump(result, target, ensure_ascii=False, indent=2)
                    target.write("\n")
                    target.flush()
                    os.fsync(target.fileno())
                result = {"plan_id": result["plan_id"], "operation": result["operation"],
                          "tenant_id": result["tenant_id"], "entries": len(result["entries"]), "dry_run": True}
        else:
            result = service.apply(body, tenant_id=args.tenant, actor=args.actor)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # Never emit source paths, source content, DSNs, SQL or raw exceptions.
        code = error.code if isinstance(error, DomainError) else "RETENTION_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
