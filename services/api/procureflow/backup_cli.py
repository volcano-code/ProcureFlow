"""Offline operator CLI. Never loads Settings or ambient credentials for restore."""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
import sys

from sqlalchemy.exc import SQLAlchemyError

from .backup import BackupError
from .backup_protection import (MODES, _keys, create_backup, read_key_file, restore_archive, verify_archive)
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
    for command in (backup, verify, restore):
        command.add_argument("--protection", choices=MODES, required=True,
                             help="Required exact protection policy; plain explicitly permits unauthenticated V1")
        command.add_argument("--encryption-key-file", type=Path,
                             help="Owner-only minimal oct/A256GCM JWK; never put key bytes in arguments")
    backup.add_argument("--signing-key-file", type=Path, help="Owner-only minimal private Ed25519 JWK")
    for command in (verify, restore):
        command.add_argument("--verification-key-file", type=Path,
                             help="Owner-only independently trusted minimal public Ed25519 JWK")
    args = parser.parse_args(argv)
    db = None
    try:
        encryption_key = read_key_file(args.encryption_key_file) if args.encryption_key_file else None
        authority_path = args.signing_key_file if args.command == "backup" else args.verification_key_file
        authority_key = read_key_file(authority_path) if authority_path else None
        # Reject unused/missing/invalid keys before opening even the source DB.
        _keys(args.protection, encryption_key, authority_key, writing=args.command == "backup")
        if args.command == "backup":
            db = Database(_database_url_file(args.database_url_file))
            result = create_backup(db, args.documents, args.output, args.source_revision,
                                   mode=args.protection, encryption_key=encryption_key, signing_key=authority_key)
        elif args.command == "verify":
            result = verify_archive(args.archive, mode=args.protection, encryption_key=encryption_key,
                                    verification_key=authority_key)
            result = {"backup_id": result.manifest["backup_id"], "state": "verified",
                      "schema_heads": result.manifest["schema_heads"], "documents": len(result.documents),
                      "backup_protection": result.protection}
        else:
            url = _database_url_file(args.postgres_url_file) if args.postgres_url_file else None
            result = restore_archive(args.archive, args.data_dir, postgres_url=url, mode=args.protection,
                                     encryption_key=encryption_key, verification_key=authority_key).report()
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
