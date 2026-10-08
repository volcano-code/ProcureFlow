"""Bounded, typed paired backups. Archive bytes are never executable SQL.

Version 1 deliberately requires the same application source and schema. The caller
must pause the source before backup. Restore only creates a new, isolated target.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
from uuid import uuid4
import zipfile

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Boolean, Integer, JSON, String, Text, create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .database_config import normalize_database_url
from .db import Base, Database

FORMAT = "procureflow-paired-backup"
VERSION = 1
# Fixed v1 ceilings are not controlled by the archive. No compression is accepted.
LIMITS = {"archive_bytes": 256 * 1024 * 1024, "database_bytes": 64 * 1024 * 1024,
          "document_bytes": 16 * 1024 * 1024, "manifest_bytes": 2 * 1024 * 1024,
          "members": 10002, "rows_per_table": 100000, "rows_total": 500000, "json_depth": 64, "json_tokens": 1000000}
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{7,64}|unversioned)\Z")
_API = Path(__file__).resolve().parents[1]


class BackupError(ValueError):
    """An operator-safe code; never includes a DSN, row or document content."""


@dataclass(frozen=True)
class VerifiedBackup:
    manifest: dict
    tables: dict[str, list[dict]]
    documents: dict[str, bytes]


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    data_dir: Path
    database: str
    schema: str | None = None
    state: str = "RECOVERY"

    def report(self) -> dict:
        return {"backup_id": self.backup_id, "data_dir": str(self.data_dir),
                "database": self.database, "schema": self.schema, "state": self.state,
                "required_auth_mode": "pilot", "old_credentials": "revoked",
                "old_operations": "held; no replay"}


def _fail(code):
    raise BackupError(code)


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _heads() -> list[str]:
    return sorted(ScriptDirectory(str(_API / "alembic")).get_heads())


def source_fingerprint() -> str:
    """Hash only executable package and migration source, never configuration."""
    sources = sorted((_API / "procureflow").rglob("*.py")) + sorted((_API / "alembic").rglob("*.py"))
    content = [[str(path.relative_to(_API)), _digest(path.read_bytes())] for path in sources]
    return _digest(_json_bytes(content))


def _schema() -> dict:
    result = {}
    for table in Base.metadata.sorted_tables:
        result[table.name] = {
            "columns": [{"name": column.name, "type": str(column.type), "nullable": column.nullable,
                         "primary_key": column.primary_key} for column in table.columns],
            "foreign_keys": sorted([{"columns": [e.parent.name for e in fk.elements],
                                     "references": [e.target_fullname for e in fk.elements]}
                                    for fk in table.foreign_key_constraints], key=lambda x: repr(x)),
            "unique": sorted([sorted(c.columns.keys()) for c in table.constraints
                              if c.__class__.__name__ == "UniqueConstraint"]),
            "indexes": sorted([{"columns": list(i.columns.keys()), "unique": bool(i.unique)}
                               for i in table.indexes], key=lambda x: repr(x)),
        }
    return result


def _source_schema(connection):
    inspector = inspect(connection)
    if set(inspector.get_table_names()) != set(Base.metadata.tables) | {"alembic_version"}:
        _fail("BACKUP_SCHEMA_TABLES_MISMATCH")
    if inspector.get_view_names():
        _fail("BACKUP_SCHEMA_VIEWS_UNSUPPORTED")
    if sorted(MigrationContext.configure(connection).get_current_heads()) != _heads():
        _fail("BACKUP_MIGRATION_REQUIRED")
    for table in Base.metadata.sorted_tables:
        actual = {c["name"]: c for c in inspector.get_columns(table.name)}
        if set(actual) != set(table.columns.keys()):
            _fail("BACKUP_SCHEMA_COLUMNS_MISMATCH")
        for col in table.columns:
            physical = actual[col.name]
            expected_type = str(col.type.compile(dialect=connection.dialect)).upper()
            if (str(physical["type"]).upper() != expected_type
                    or physical["nullable"] != col.nullable):
                _fail("BACKUP_SCHEMA_TYPES_MISMATCH")
        if set(inspector.get_pk_constraint(table.name)["constrained_columns"]) != set(table.primary_key.columns.keys()):
            _fail("BACKUP_SCHEMA_KEYS_MISMATCH")
        expected_fk = {(tuple(e.parent.name for e in fk.elements),
                        tuple(e.target_fullname for e in fk.elements)) for fk in table.foreign_key_constraints}
        actual_fk = {(tuple(fk["constrained_columns"]),
                      tuple(f'{fk["referred_table"]}.{col}' for col in fk["referred_columns"]))
                     for fk in inspector.get_foreign_keys(table.name)}
        if actual_fk != expected_fk:
            _fail("BACKUP_SCHEMA_KEYS_MISMATCH")
        current_schema = inspector.default_schema_name
        if any(fk.get("referred_schema") not in {None, current_schema}
               for fk in inspector.get_foreign_keys(table.name)):
            _fail("BACKUP_SCHEMA_KEYS_MISMATCH")
        actual_unique = {tuple(sorted(item["column_names"]))
                         for item in inspector.get_unique_constraints(table.name)}
        expected_unique = {tuple(sorted(item.columns.keys())) for item in table.constraints
                           if item.__class__.__name__ == "UniqueConstraint"}
        actual_indexes = {(tuple(item["column_names"]), bool(item["unique"]))
                          for item in inspector.get_indexes(table.name) if not item.get("duplicates_constraint")}
        expected_indexes = {(tuple(item.columns.keys()), bool(item.unique)) for item in table.indexes}
        if actual_unique != expected_unique or actual_indexes != expected_indexes:
            _fail("BACKUP_SCHEMA_KEYS_MISMATCH")
    if connection.dialect.name == "sqlite":
        if connection.execute(text("PRAGMA integrity_check")).scalars().all() != ["ok"]:
            _fail("BACKUP_DATABASE_INTEGRITY")
        if connection.execute(text("PRAGMA foreign_key_check")).first() is not None:
            _fail("BACKUP_DATABASE_REFERENCES")


def _key(key):
    if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key):
        _fail("BACKUP_UNSAFE_STORAGE_KEY")
    return key


def _references(tables) -> dict[str, str]:
    references = {}
    for name in ("documents", "table_imports"):
        for row in tables[name]:
            key, digest = _key(row["storage_key"]), row["sha256"]
            if not isinstance(digest, str) or not _HEX.fullmatch(digest):
                _fail("BACKUP_INVALID_DOCUMENT_HASH")
            if key in references and references[key] != digest:
                _fail("BACKUP_DOCUMENT_REFERENCE_CONFLICT")
            references[key] = digest
    return references


def _read_document(directory: Path, key: str) -> bytes:
    fd = None
    try:
        fd = os.open(directory / key, os.O_RDONLY | os.O_NOFOLLOW)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > LIMITS["document_bytes"]:
            _fail("BACKUP_DOCUMENT_INVALID")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            result = stream.read(LIMITS["document_bytes"] + 1)
            after = os.fstat(stream.fileno())
        if (len(result) != before.st_size or len(result) > LIMITS["document_bytes"]
                or (before.st_ino, before.st_mtime_ns, before.st_size) !=
                   (after.st_ino, after.st_mtime_ns, after.st_size)):
            _fail("BACKUP_DOCUMENT_CHANGED")
        return result
    except OSError:
        _fail("BACKUP_DOCUMENT_UNREADABLE")
    finally:
        if fd is not None:
            os.close(fd)


def backup_database(db: Database, document_dir: Path, output: Path, source_revision="unversioned") -> dict:
    """Create an exclusive new bundle from a paused source under its maintenance fence."""
    if not isinstance(source_revision, str) or not _REVISION.fullmatch(source_revision):
        _fail("BACKUP_INVALID_SOURCE_REVISION")
    output, document_dir = Path(output), Path(document_dir)
    if os.path.lexists(output):
        _fail("BACKUP_OUTPUT_EXISTS")
    if document_dir.is_symlink() or not document_dir.is_dir():
        _fail("BACKUP_DOCUMENT_DIRECTORY_INVALID")
    if os.path.lexists(document_dir.parent / ".restore-incomplete"):
        _fail("BACKUP_INCOMPLETE_SOURCE")
    temporary = None
    try:
        with db.maintenance() as session:
            connection = session.connection()
            _source_schema(connection)
            tables, total, logical_bytes = {}, 0, 0
            for table in Base.metadata.sorted_tables:
                rows = []
                query = select(table).order_by(*table.primary_key.columns).limit(LIMITS["rows_per_table"] + 1)
                for row in session.execute(query.execution_options(yield_per=500)).mappings():
                    row = dict(row)
                    logical_bytes += len(_json_bytes(row))
                    if logical_bytes > LIMITS["database_bytes"]:
                        _fail("BACKUP_DATABASE_LIMIT")
                    rows.append(row)
                if len(rows) > LIMITS["rows_per_table"]:
                    _fail("BACKUP_ROW_LIMIT")
                tables[table.name] = rows
                total += len(rows)
            if total > LIMITS["rows_total"]:
                _fail("BACKUP_ROW_LIMIT")
            _validate_tables(tables)
            data = _json_bytes({"tables": tables})
            if len(data) > LIMITS["database_bytes"]:
                _fail("BACKUP_DATABASE_LIMIT")
            _check_json_budget(data)  # Never emit a bundle the strict reader would reject.
            docs, metadata, size = {}, {}, len(data)
            for key, expected in sorted(_references(tables).items()):
                content = _read_document(document_dir, key)
                if _digest(content) != expected:
                    _fail("BACKUP_DOCUMENT_HASH_MISMATCH")
                docs[key] = content
                metadata[key] = {"sha256": expected, "bytes": len(content)}
                size += len(content)
                if size > LIMITS["archive_bytes"] or len(docs) + 2 > LIMITS["members"]:
                    _fail("BACKUP_ARCHIVE_LIMIT")
            schema = _schema()
            manifest = {"format": FORMAT, "version": VERSION, "backup_id": uuid4().hex,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "source": {"revision": source_revision, "fingerprint": source_fingerprint(),
                                   "database": "sqlite" if db.sqlite else "postgresql"},
                        "schema_heads": _heads(), "schema": schema, "schema_sha256": _digest(_json_bytes(schema)),
                        "limits": LIMITS, "database": {"sha256": _digest(data), "bytes": len(data),
                                                       "rows": {key: len(value) for key, value in tables.items()}},
                        "documents": metadata}
            manifest_bytes = _json_bytes(manifest)
            if len(manifest_bytes) > LIMITS["manifest_bytes"]:
                _fail("BACKUP_MANIFEST_LIMIT")
            _check_json_budget(manifest_bytes)
            fd, name = tempfile.mkstemp(prefix=".pf-backup-", dir=output.parent)
            temporary = Path(name)
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
                    archive.writestr("manifest.json", manifest_bytes)
                    archive.writestr("database.json", data)
                    for key, content in docs.items():
                        archive.writestr("documents/" + key, content)
                if stream.tell() > LIMITS["archive_bytes"]:
                    _fail("BACKUP_ARCHIVE_LIMIT")
                stream.flush()
                os.fsync(stream.fileno())
            # Read back and check the actual bundle before publishing it.
            verify_backup(temporary)
            # Atomic and exclusive: a concurrently created output is never replaced.
            os.link(temporary, output)
        return {"backup_id": manifest["backup_id"], "output": str(output),
                "tables": len(tables), "rows": total, "documents": len(docs), "state": "verified"}
    except (OSError, SQLAlchemyError, zipfile.BadZipFile):
        raise BackupError("BACKUP_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("BACKUP_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _check_json_budget(content):
    # Bound parser object amplification and nesting before allocating JSON trees.
    depth, tokens, quoted, escaped = 0, 0, False, False
    for char in content:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if char == 34:
            quoted = True
            tokens += 1
        elif char in (91, 123):
            depth += 1
            tokens += 1
            if depth > LIMITS["json_depth"]:
                _fail("BACKUP_JSON_DEPTH_LIMIT")
        elif char in (93, 125):
            depth -= 1
        elif char in (44, 58):
            tokens += 1
        if tokens > LIMITS["json_tokens"]:
            _fail("BACKUP_JSON_TOKEN_LIMIT")


def _load_json(content):
    _check_json_budget(content)
    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: _fail("BACKUP_INVALID_JSON_NUMBER"))
    except BackupError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("BACKUP_INVALID_JSON")


def _json_value(value, depth=0):
    if depth > LIMITS["json_depth"]:
        _fail("BACKUP_JSON_DEPTH_LIMIT")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            _fail("BACKUP_INVALID_STRING")
        return
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for element in value:
            _json_value(element, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, element in value.items():
            _json_value(key, depth + 1)
            _json_value(element, depth + 1)
        return
    _fail("BACKUP_INVALID_JSON_VALUE")


def _validate_tables(tables):
    if not isinstance(tables, dict) or set(tables) != set(Base.metadata.tables):
        _fail("BACKUP_SCHEMA_TABLES_MISMATCH")
    total = 0
    for table in Base.metadata.sorted_tables:
        rows = tables[table.name]
        if not isinstance(rows, list) or len(rows) > LIMITS["rows_per_table"]:
            _fail("BACKUP_ROW_LIMIT")
        total += len(rows)
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(table.columns.keys()):
                _fail("BACKUP_SCHEMA_COLUMNS_MISMATCH")
            for column in table.columns:
                value = row[column.name]
                if value is None:
                    if not column.nullable:
                        _fail("BACKUP_NULL_REQUIRED_VALUE")
                elif isinstance(column.type, Boolean):
                    if type(value) is not bool:
                        _fail("BACKUP_INVALID_BOOLEAN")
                elif isinstance(column.type, Integer):
                    if type(value) is not int or not -(2**31) <= value < 2**31:
                        _fail("BACKUP_INVALID_INTEGER")
                elif isinstance(column.type, JSON):
                    _json_value(value)
                elif isinstance(column.type, (String, Text)):
                    if not isinstance(value, str) or (column.type.length and len(value) > column.type.length) or "\x00" in value:
                        _fail("BACKUP_INVALID_STRING")
                    _json_value(value)
                else:
                    _fail("BACKUP_UNSUPPORTED_COLUMN_TYPE")
    if total > LIMITS["rows_total"]:
        _fail("BACKUP_ROW_LIMIT")
    states = tables.get("system_state", [])
    if len(states) != 1 or states[0]["id"] != 1 or states[0]["state"] not in {"ACTIVE", "PAUSED", "RECOVERY"}:
        _fail("BACKUP_INVALID_RECOVERY_STATE")
    # Validate every local FK, primary/unique key and required value before a target
    # exists. DDL comes exclusively from installed models, never archive content.
    engine = create_engine("sqlite:///:memory:")
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            _insert_tables(connection, tables)
            if connection.execute(text("PRAGMA integrity_check")).scalar() != "ok":
                _fail("BACKUP_DATABASE_INTEGRITY")
            if connection.execute(text("PRAGMA foreign_key_check")).first() is not None:
                _fail("BACKUP_DATABASE_REFERENCES")
    except SQLAlchemyError:
        _fail("BACKUP_DATABASE_CONSTRAINTS")
    finally:
        engine.dispose()


def _insert_tables(connection, tables):
    for table in Base.metadata.sorted_tables:
        rows = tables[table.name]
        for start in range(0, len(rows), 500):
            connection.execute(table.insert(), rows[start:start + 500])


def _preflight_zip(stream):
    """Bound the central directory before ZipFile allocates one object per member.

    V1 is deliberately canonical: classic single-disk ZIP, stored entries, no
    extra fields, comments, data descriptors, prefix, trailing bytes or ZIP64.
    Both the advertised count and the actual directory records are validated.
    """
    size = os.fstat(stream.fileno()).st_size
    if size < 22 or size > LIMITS["archive_bytes"]:
        _fail("BACKUP_ARCHIVE_LIMIT")
    stream.seek(size - 22)
    end = struct.unpack("<4s4H2LH", stream.read(22))
    signature, disk, directory_disk, disk_count, count, directory_bytes, directory_offset, comment = end
    if signature != b"PK\x05\x06" or disk or directory_disk or disk_count != count or comment:
        _fail("BACKUP_ARCHIVE_INVALID")
    if count > LIMITS["members"] or directory_bytes > LIMITS["members"] * (46 + 110):
        _fail("BACKUP_ARCHIVE_LIMIT")
    if directory_offset + directory_bytes != size - 22 or count < 2:
        _fail("BACKUP_ARCHIVE_INVALID")
    offset, local_end = directory_offset, 0
    for _ in range(count):
        if offset + 46 > size - 22:
            _fail("BACKUP_ARCHIVE_INVALID")
        stream.seek(offset)
        central = struct.unpack("<4s6H3L5H2L", stream.read(46))
        (_, _, needed, flags, method, _, _, crc, compressed, expanded,
         name_size, extra_size, comment_size, start_disk, _, _, local_offset) = central
        if (central[0] != b"PK\x01\x02" or needed > 20 or flags or method != zipfile.ZIP_STORED
                or compressed != expanded or not 1 <= name_size <= 110 or extra_size or comment_size
                or start_disk or local_offset != local_end):
            _fail("BACKUP_UNSAFE_MEMBER")
        name = stream.read(name_size)
        if len(name) != name_size or offset + 46 + name_size > size - 22:
            _fail("BACKUP_ARCHIVE_INVALID")
        if local_offset + 30 + name_size + compressed > directory_offset:
            _fail("BACKUP_ARCHIVE_INVALID")
        stream.seek(local_offset)
        local = struct.unpack("<4s5H3L2H", stream.read(30))
        if (local[0] != b"PK\x03\x04" or local[1] != needed or local[2] != flags or local[3] != method
                or local[6] != crc or local[7] != compressed or local[8] != expanded
                or local[9] != name_size or local[10] != 0 or stream.read(name_size) != name):
            _fail("BACKUP_UNSAFE_MEMBER")
        local_end = local_offset + 30 + name_size + compressed
        offset += 46 + name_size
    if offset != size - 22 or local_end != directory_offset:
        _fail("BACKUP_ARCHIVE_INVALID")
    stream.seek(0)


def verify_backup(path: Path) -> VerifiedBackup:
    """Verify all bytes, schema, types, references and constraints without any target."""
    try:
        path = Path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _fail("BACKUP_ARCHIVE_INVALID")
            _preflight_zip(stream)
            return _verify_open_archive(stream)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OverflowError, struct.error):
        _fail("BACKUP_ARCHIVE_INVALID")


def _verify_open_archive(stream):
    with zipfile.ZipFile(stream, "r") as archive:
        members = archive.infolist()
        if len(members) > LIMITS["members"]:
            _fail("BACKUP_ARCHIVE_LIMIT")
        names = [member.filename for member in members]
        if len(names) != len(set(names)):
            _fail("BACKUP_DUPLICATE_MEMBER")
        total = 0
        for member in members:
            name = member.filename
            mode = member.external_attr >> 16
            if (name != member.orig_filename or member.is_dir()
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                    or member.compress_type != zipfile.ZIP_STORED or member.flag_bits & 1
                    or member.compress_size != member.file_size):
                _fail("BACKUP_UNSAFE_MEMBER")
            limit = LIMITS["document_bytes"]
            if name == "manifest.json":
                limit = LIMITS["manifest_bytes"]
            elif name == "database.json":
                limit = LIMITS["database_bytes"]
            elif name.startswith("documents/"):
                _key(name[len("documents/"):])
            else:
                _fail("BACKUP_UNEXPECTED_MEMBER")
            total += member.file_size
            if member.file_size > limit or total > LIMITS["archive_bytes"]:
                _fail("BACKUP_ARCHIVE_LIMIT")
        if "manifest.json" not in names or "database.json" not in names:
            _fail("BACKUP_REQUIRED_MEMBER_MISSING")
        manifest = _load_json(archive.read("manifest.json"))
        _validate_manifest(manifest)
        expected = {"manifest.json", "database.json"} | {"documents/" + key for key in manifest["documents"]}
        if set(names) != expected:
            _fail("BACKUP_DOCUMENT_SET_MISMATCH")
        data = archive.read("database.json")
        if len(data) != manifest["database"]["bytes"] or _digest(data) != manifest["database"]["sha256"]:
            _fail("BACKUP_DATABASE_HASH_MISMATCH")
        parsed = _load_json(data)
        if not isinstance(parsed, dict) or set(parsed) != {"tables"}:
            _fail("BACKUP_INVALID_DATABASE")
        tables = parsed["tables"]
        _validate_tables(tables)
        if manifest["database"]["rows"] != {key: len(rows) for key, rows in tables.items()}:
            _fail("BACKUP_ROW_COUNT_MISMATCH")
        references = _references(tables)
        if set(references) != set(manifest["documents"]):
            _fail("BACKUP_DOCUMENT_SET_MISMATCH")
        documents = {}
        for key, metadata in manifest["documents"].items():
            content = archive.read("documents/" + key)
            if len(content) != metadata["bytes"] or _digest(content) != metadata["sha256"] or references[key] != metadata["sha256"]:
                _fail("BACKUP_DOCUMENT_HASH_MISMATCH")
            documents[key] = content
        return VerifiedBackup(manifest, tables, documents)


def _validate_manifest(manifest):
    keys = {"format", "version", "backup_id", "created_at", "source", "schema_heads", "schema", "schema_sha256",
            "limits", "database", "documents"}
    if not isinstance(manifest, dict) or set(manifest) != keys:
        _fail("BACKUP_INVALID_MANIFEST")
    if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        _fail("BACKUP_UNSUPPORTED_VERSION")
    if not isinstance(manifest["backup_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", manifest["backup_id"]):
        _fail("BACKUP_INVALID_MANIFEST")
    try:
        stamp = datetime.fromisoformat(manifest["created_at"])
        if stamp.tzinfo is None:
            _fail("BACKUP_INVALID_MANIFEST")
    except (TypeError, ValueError):
        _fail("BACKUP_INVALID_MANIFEST")
    source = manifest["source"]
    if (not isinstance(source, dict) or set(source) != {"revision", "fingerprint", "database"}
            or not isinstance(source["revision"], str) or not _REVISION.fullmatch(source["revision"])
            or not isinstance(source["database"], str) or source["database"] not in {"sqlite", "postgresql"}):
        _fail("BACKUP_INVALID_MANIFEST")
    if source["fingerprint"] != source_fingerprint():
        _fail("BACKUP_SOURCE_VERSION_MISMATCH")
    schema = _schema()
    if (manifest["schema_heads"] != _heads() or manifest["schema"] != schema
            or manifest["schema_sha256"] != _digest(_json_bytes(schema))):
        _fail("BACKUP_SCHEMA_VERSION_MISMATCH")
    if manifest["limits"] != LIMITS:
        _fail("BACKUP_LIMITS_MISMATCH")
    database = manifest["database"]
    if not isinstance(database, dict) or set(database) != {"sha256", "bytes", "rows"}:
        _fail("BACKUP_INVALID_MANIFEST")
    _member_metadata({"sha256": database["sha256"], "bytes": database["bytes"]}, LIMITS["database_bytes"])
    if (not isinstance(database["rows"], dict) or set(database["rows"]) != set(Base.metadata.tables)
            or any(type(n) is not int or not 0 <= n <= LIMITS["rows_per_table"] for n in database["rows"].values())):
        _fail("BACKUP_INVALID_MANIFEST")
    if not isinstance(manifest["documents"], dict) or len(manifest["documents"]) + 2 > LIMITS["members"]:
        _fail("BACKUP_INVALID_MANIFEST")
    for key, metadata in manifest["documents"].items():
        _key(key)
        _member_metadata(metadata, LIMITS["document_bytes"])


def _member_metadata(value, limit):
    if (not isinstance(value, dict) or set(value) != {"sha256", "bytes"}
            or not isinstance(value["sha256"], str) or not _HEX.fullmatch(value["sha256"])
            or type(value["bytes"]) is not int or not 0 <= value["bytes"] <= limit):
        _fail("BACKUP_INVALID_MANIFEST")


def _recovery_tables(verified):
    tables = {name: [dict(row) for row in rows] for name, rows in verified.tables.items()}
    stamp, identifier = datetime.now(timezone.utc).isoformat(), verified.manifest["backup_id"]
    for name in ("pilot_invites", "pilot_sessions"):
        for row in tables[name]:
            row["revoked_at"] = row["revoked_at"] or stamp
    for name in ("pilot_tenants", "pilot_memberships"):
        for row in tables[name]:
            row["generation"] += 1
    state = tables["system_state"][0]
    state.update(state="RECOVERY", generation=state["generation"] + 1, reason="Restored backup; operator review required",
                 restore_id=identifier, required_auth_mode="pilot", updated_at=stamp)
    held = {row["operation_id"] for row in tables["recovery_holds"]}
    for row in tables["external_operations"]:
        if row["status"] != "COMPLETED" and row["id"] not in held:
            tables["recovery_holds"].append({"operation_id": row["id"], "restore_id": identifier,
                                            "original_status": row["status"], "created_at": stamp})
    next_event = max((row["id"] for row in tables["audit_events"]), default=0) + 1
    tables["audit_events"].append({"id": next_event, "tenant_id": "__operator__", "request_id": None,
                                  "actor_id": "offline-operator", "type": "BACKUP_RESTORED",
                                  "payload": {"backup_id": identifier,
                                              "manifest_sha256": _digest(_json_bytes(verified.manifest)),
                                              "original_database_sha256": verified.manifest["database"]["sha256"],
                                              "required_auth_mode": "pilot", "automatic_replay_enabled": False},
                                  "created_at": stamp})
    _validate_tables(tables)
    return tables


def _migrate(db, recovery_id=None):
    cfg = Config(str(_API / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API / "alembic"))
    with db.engine.begin() as connection:
        if db.sqlite:
            # SQLite DDL must participate in the same transaction as the fence.
            connection.execute(text("BEGIN IMMEDIATE"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        if recovery_id is not None:
            connection.execute(Base.metadata.tables["system_state"].update().values(
                state="RECOVERY", reason="Incomplete restore; operator review required", restore_id=recovery_id,
                required_auth_mode="pilot", updated_at=datetime.now(timezone.utc).isoformat()))


def restore_backup(path: Path, data_dir: Path, *, postgres_url: str | None = None) -> RestoreResult:
    """Verify first, then create only a fresh local directory and optional random PG schema.

    postgres_url is explicit operator input, never read from environment or archive.
    Existing databases/tables/directories are never a restore destination.
    """
    verified = verify_backup(path)
    tables = _recovery_tables(verified)
    target = Path(data_dir).absolute()
    if os.path.lexists(target):
        _fail("RESTORE_TARGET_EXISTS")
    if not target.parent.is_dir() or target.parent.is_symlink():
        _fail("RESTORE_PARENT_INVALID")
    parsed = None
    if postgres_url is not None:
        try:
            parsed = make_url(normalize_database_url(postgres_url))
            if parsed.get_backend_name() != "postgresql":
                _fail("RESTORE_POSTGRES_URL_REQUIRED")
        except (ValueError, TypeError):
            _fail("RESTORE_POSTGRES_URL_REQUIRED")
    schema, admin, db = None, None, None
    try:
        target.mkdir(mode=0o700)
        marker = target / ".restore-incomplete"
        def mark_incomplete():
            # Intentionally retained after failure; no automatic purge of a path
            # or PostgreSQL schema, even one originally created by this call.
            marker.write_text(json.dumps({"backup_id": verified.manifest["backup_id"],
                                          "state": "INCOMPLETE", "schema": schema}) + "\n", encoding="utf-8")
            os.chmod(marker, 0o600)
        mark_incomplete()
        (target / "documents").mkdir(mode=0o700)
        for key, content in verified.documents.items():
            descriptor = os.open(target / "documents" / key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        if parsed is None:
            db = Database(f"sqlite:///{target / 'procureflow.sqlite3'}")
        else:
            schema = "pf_restore_" + uuid4().hex
            mark_incomplete()
            admin_url = parsed.update_query_dict({"options": "-csearch_path=pg_catalog"})
            admin = create_engine(admin_url, hide_parameters=True, connect_args={"connect_timeout": 10})
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            isolated = parsed.update_query_dict({"options": f"-csearch_path={schema}"})
            db = Database(isolated.render_as_string(hide_password=False))
        _migrate(db, recovery_id=verified.manifest["backup_id"])
        # Trusted migrations create only the runtime singleton; replace that row.
        # Import bypasses runtime gating only on this newly constructed target.
        with db.engine.begin() as connection:
            connection.execute(Base.metadata.tables["system_state"].delete())
            _insert_tables(connection, tables)
            if parsed is not None:
                for table in Base.metadata.sorted_tables:
                    for col in table.primary_key.columns:
                        if isinstance(col.type, Integer) and col.autoincrement is not False:
                            # Names are compiled trusted model identifiers, never archive SQL.
                            connection.execute(text("SELECT setval(pg_get_serial_sequence(:table, :column), "
                                                    f'COALESCE((SELECT MAX("{col.name}") FROM "{table.name}"), 1), '
                                                    f'EXISTS(SELECT 1 FROM "{table.name}"))'),
                                               {"table": table.name, "column": col.name})
            _source_schema(connection)
        if parsed is None:
            os.chmod(target / "procureflow.sqlite3", 0o600)
        result = RestoreResult(verified.manifest["backup_id"], target, "postgresql" if parsed else "sqlite", schema)
        with (target / "recovery-report.json").open("x", encoding="utf-8") as stream:
            json.dump(result.report(), stream, indent=2)
            stream.write("\n")
        os.chmod(target / "recovery-report.json", 0o600)
        marker.unlink()
        return result
    except (OSError, SQLAlchemyError):
        raise BackupError("RESTORE_FAILED_INCOMPLETE") from None
    finally:
        if db is not None:
            db.engine.dispose()
        if admin is not None:
            admin.dispose()
