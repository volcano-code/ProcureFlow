"""Offline operator CLI. Never loads Settings or ambient credentials for restore."""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
import sys

from sqlalchemy.exc import SQLAlchemyError

from .backup import BackupError, backup_database, restore_backup, verify_backup
from .db import Database
from .errors import DomainError


def _database_url_file(path):
    """Require an explicit owner-only regular file; do not echo its contents."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > 8192):
            raise BackupError("DATABASE_URL_FILE_MUST_BE_OWNER_ONLY")
        value = stream.read(8193).strip()
        if not value or len(value) > 8192 or "\n" in value or "\r" in value:
            raise BackupError("DATABASE_URL_FILE_INVALID")
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded paired backup and isolated recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Snapshot an already paused source")
    backup.add_argument("--database-url-file", type=Path, required=True, help="Owner-only file containing source URL")
    backup.add_argument("--documents", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--source-revision", default="unversioned")
    verify = commands.add_parser("verify", help="Verify without touching a restore destination")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore", help="Restore to a new directory and optional new random PostgreSQL schema")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--data-dir", type=Path, required=True)
    restore.add_argument("--postgres-url-file", type=Path, help="Owner-only URL file; creates only a new random schema")
    args = parser.parse_args(argv)
    db = None
    try:
        if args.command == "backup":
            db = Database(_database_url_file(args.database_url_file))
            result = backup_database(db, args.documents, args.output, args.source_revision)
        elif args.command == "verify":
            result = verify_backup(args.archive)
            result = {"backup_id": result.manifest["backup_id"], "state": "verified",
                      "schema_heads": result.manifest["schema_heads"], "documents": len(result.documents)}
        else:
            url = _database_url_file(args.postgres_url_file) if args.postgres_url_file else None
            result = restore_backup(args.archive, args.data_dir, postgres_url=url).report()
        print(json.dumps(result, sort_keys=True))
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    except BackupError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (RuntimeError, ValueError, SQLAlchemyError, OSError):
        # Connection strings, bind values and archive content never reach terminal.
        print(json.dumps({"error": "BACKUP_OPERATOR_ACTION_FAILED"}), file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
