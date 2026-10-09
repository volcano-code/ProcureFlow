"""Synthetic fixtures only: hostile bundles, fresh restores and durable replay holds."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest
from sqlalchemy import select, text

from procureflow.backup import (BackupError, LIMITS, _json_bytes, _migrate, backup_database,
                               restore_backup, verify_backup)
from procureflow.backup_cli import main
from procureflow.db import (Base, Database, DocumentRow, EventRow, OperationRow, OutboxRow, RequestRow,
                           SystemStateRow)
from procureflow.errors import DomainError
from procureflow.maintenance import pause_writes, recovery_report, resume_writes


STAMP = "2026-01-01T00:00:00+00:00"


def _seed(db, documents):
    documents.mkdir()
    blob = b"Synthetic procurement document, not real user information\n"
    (documents / "doc-fixture.txt").write_bytes(blob)
    data = {name: [] for name in Base.metadata.tables}
    def add(name, **kwargs):
        table = Base.metadata.tables[name]
        row = {}
        for column in table.columns:
            if column.name in kwargs:
                row[column.name] = kwargs[column.name]
            elif column.nullable:
                row[column.name] = None
            elif column.name in {"created_at", "updated_at", "effective_at"}:
                row[column.name] = STAMP
            elif column.default is not None and column.default.is_scalar:
                row[column.name] = column.default.arg
            else:
                raise AssertionError(f"Fixture must set {name}.{column.name}")
        data[name].append(row)
    add("tenant_policies", tenant_id="synthetic", latest_version=1)
    add("pilot_tenants", tenant_id="synthetic", active=True, generation=4)
    add("pilot_memberships", tenant_id="synthetic", user_id="buyer", role="buyer", active=True, generation=7)
    add("pilot_invites", id="invite", tenant_id="synthetic", user_id="buyer", credential_hash="a" * 64,
        auth_version="4:7", expires_at="2099-01-01T00:00:00+00:00")
    add("pilot_sessions", id="session", tenant_id="synthetic", user_id="buyer", token_hash="b" * 64,
        auth_version="4:7", expires_at="2099-01-01T00:00:00+00:00")
    add("policy_versions", id="policy", tenant_id="synthetic", version=1, data={"budget_cap": "1000"},
        policy_hash="e" * 64, created_by="approver", reason="Synthetic policy")
    statuses = ["PENDING", "IN_FLIGHT", "RECONCILING", "FAILED", "MANUAL_REVIEW", "COMPLETED"]
    for index, status in enumerate(statuses):
        rid = f"request-{index}"
        add("procurement_requests", id=rid, tenant_id="synthetic", owner_id="buyer", data={"title": "Synthetic"},
            version=3, status="EXECUTING", proposal={"snapshot_hash": "c" * 64})
        add("approvals", id=f"approval-{index}", request_id=rid, tenant_id="synthetic", approver_id="approver",
            approver_auth_version="1:1", snapshot_hash="c" * 64, snapshot={"data": "evidence"}, status="APPROVED",
            note="", expires_at="2099-01-01T00:00:00+00:00")
        add("external_operations", id=f"operation-{index}", tenant_id="synthetic", request_id=rid,
            approval_id=f"approval-{index}", initiator_id="buyer", initiator_auth_version="4:7",
            payload={"lines": [{"qty": "1"}]}, snapshot_hash="c" * 64, status=status, attempts=2,
            lease_until="2099-01-01T00:00:00+00:00", remote_id="ERP-001" if status == "COMPLETED" else None)
        add("outbox", id=f"outbox-{index}", operation_id=f"operation-{index}", status=status)
    add("documents", id="doc-fixture", tenant_id="synthetic", request_id="request-0", filename="fixture.txt",
        sha256=hashlib.sha256(blob).hexdigest(), storage_key="doc-fixture.txt", fragments=[{"text": "Synthetic"}])
    add("quotes", id="quote", tenant_id="synthetic", request_id="request-0", current_version=1)
    add("quote_versions", id="quote-version", quote_id="quote", document_id="doc-fixture", version=1,
        values={"price": "10.00"}, evidence={"document_id": "doc-fixture"}, issues=[], confirmed_by="buyer")
    add("evaluations", id="evaluation", tenant_id="synthetic", request_id="request-0", actor_id="buyer",
        request_version=3, input_hash="f" * 64, input_snapshot={"quotes": ["quote"]}, result={"winner": "quote"})
    add("audit_events", id=50, tenant_id="synthetic", request_id="request-0", actor_id="buyer",
        type="SYNTHETIC_EVIDENCE", payload={"evidence": "keep"})
    add("advice_runs", id="advice", tenant_id="synthetic", request_id="request-0", actor_id="buyer",
        idempotency_key="advice-key", request_version=3, input_hash="f" * 64,
        input_snapshot={"evidence": "keep"}, status="SUCCEEDED", output={"note": "Advisory only"})
    add("table_imports", id="preview", tenant_id="synthetic", request_id="request-0", filename="fixture.csv",
        sha256=hashlib.sha256(blob).hexdigest(), storage_key="doc-fixture.txt", table={"rows": [["Synthetic"]]},
        revision=1, request_version=3, status="OPEN", expires_at="2099-01-01T00:00:00+00:00")
    with db.engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if data[table.name]:
                connection.execute(table.insert(), data[table.name])
    return blob


@pytest.fixture
def source(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'source.sqlite3'}")
    _migrate(db)
    documents = tmp_path / "documents"
    blob = _seed(db, documents)
    pause_writes(db)
    try:
        yield db, documents, blob
    finally:
        db.engine.dispose()


@pytest.fixture
def bundle(source, tmp_path):
    db, documents, _ = source
    path = tmp_path / "backup.pfb"
    backup_database(db, documents, path, source_revision="1234567")
    return path


def _rewrite(bundle, path, *, mutate_manifest=None, mutate_database=None, extras=(), omit=(), raw_database=None):
    with zipfile.ZipFile(bundle) as archive:
        entries = {member.filename: archive.read(member.filename) for member in archive.infolist()}
    manifest = json.loads(entries["manifest.json"])
    if mutate_database:
        database = json.loads(entries["database.json"])
        mutate_database(database)
        entries["database.json"] = _json_bytes(database)
    if raw_database is not None:
        entries["database.json"] = raw_database
    if mutate_database or raw_database is not None:
        manifest["database"]["sha256"] = hashlib.sha256(entries["database.json"]).hexdigest()
        manifest["database"]["bytes"] = len(entries["database.json"])
    if mutate_manifest:
        mutate_manifest(manifest)
    entries["manifest.json"] = _json_bytes(manifest)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for key, value in entries.items():
            if key not in omit:
                archive.writestr(key, value)
        for key, value in extras:
            archive.writestr(key, value)
    return path


def test_pair_roundtrip_preserves_evidence_and_recovery_posture(source, bundle, tmp_path):
    original = verify_backup(bundle)
    target = tmp_path / "restored"
    result = restore_backup(bundle, target)
    assert result.state == "RECOVERY" and result.database == "sqlite"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / "documents" / "doc-fixture.txt").read_bytes() == source[2]
    db = Database(f"sqlite:///{target / 'procureflow.sqlite3'}")
    try:
        assert db.check_ready()["schema"] == "current"
        with db.transaction() as session:
            for name in ("procurement_requests", "documents", "quotes", "quote_versions", "approvals",
                         "external_operations", "outbox", "evaluations", "policy_versions", "advice_runs",
                         "table_imports"):
                table = Base.metadata.tables[name]
                actual = [dict(row) for row in session.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                assert actual == original.tables[name], name
            events = session.execute(select(EventRow.__table__).order_by(EventRow.id)).mappings().all()
            assert [dict(row) for row in events[:-1]] == original.tables["audit_events"]
            assert events[-1]["type"] == "BACKUP_RESTORED"
            for name in ("pilot_invites", "pilot_sessions"):
                rows = session.execute(select(Base.metadata.tables[name])).mappings().all()
                assert all(row["revoked_at"] for row in rows)
            assert session.execute(select(Base.metadata.tables["pilot_tenants"].c.generation)).scalar() == 5
            assert session.execute(select(Base.metadata.tables["pilot_memberships"].c.generation)).scalar() == 8
        report = recovery_report(db)
        assert report["state"] == "RECOVERY" and report["required_auth_mode"] == "pilot"
        assert report["automatic_replay_enabled"] is False
        assert len([row for row in report["operations"] if row["hold_restore_id"]]) == 5
        with pytest.raises(DomainError, match="Writes are paused"):
            with db.transaction(write=True):
                pass
        with pytest.raises(DomainError) as caught:
            resume_writes(db, generation=report["generation"], ledger_sha256=report["ledger_sha256"])
        assert caught.value.code == "RECOVERY_REVIEW_REQUIRED"
        resumed = resume_writes(db, generation=report["generation"], ledger_sha256=report["ledger_sha256"],
                                restore_id=result.backup_id, acknowledge_reconciliation=True, acknowledge_credentials=True)
        assert resumed["state"] == "ACTIVE"
        assert resumed["required_auth_mode"] == "pilot"
        assert len([row for row in resumed["operations"] if row["hold_restore_id"]]) == 5
    finally:
        db.engine.dispose()


def test_backup_requires_explicit_pause(source, tmp_path):
    db, docs, _ = source
    report = recovery_report(db)
    resume_writes(db, generation=report["generation"], ledger_sha256=report["ledger_sha256"])
    with pytest.raises(DomainError) as caught:
        backup_database(db, docs, tmp_path / "rejected.pfb")
    assert caught.value.code == "MAINTENANCE_PAUSE_REQUIRED"
    assert not (tmp_path / "rejected.pfb").exists()


def test_backup_never_replaces_output(source, bundle):
    before = bundle.read_bytes()
    with pytest.raises(BackupError, match="BACKUP_OUTPUT_EXISTS"):
        backup_database(source[0], source[1], bundle)
    assert bundle.read_bytes() == before


def test_restore_never_replaces_existing_target(bundle, tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "precious.txt"
    sentinel.write_text("unchanged")
    with pytest.raises(BackupError, match="RESTORE_TARGET_EXISTS"):
        restore_backup(bundle, target)
    assert sentinel.read_text() == "unchanged" and list(target.iterdir()) == [sentinel]
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(BackupError, match="RESTORE_TARGET_EXISTS"):
        restore_backup(bundle, alias)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "documents/../escape", "documents/sub/file", "documents/x\\y", "database.sql"])
def test_hostile_names_fail_before_target(bundle, tmp_path, name):
    bad = _rewrite(bundle, tmp_path / "hostile.pfb", extras=[(name, b"DROP TABLE important;")])
    target = tmp_path / "must-not-exist"
    with pytest.raises(BackupError):
        restore_backup(bad, target)
    assert not target.exists()


def test_duplicate_member_rejected(bundle, tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        bad = _rewrite(bundle, tmp_path / "duplicate.pfb", extras=[("database.json", b"{}")])
    with pytest.raises(BackupError, match="BACKUP_DUPLICATE_MEMBER"):
        verify_backup(bad)


def test_symlink_member_rejected(bundle, tmp_path):
    member = zipfile.ZipInfo("documents/link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    bad = _rewrite(bundle, tmp_path / "symlink.pfb", extras=[(member, b"/etc/passwd")])
    with pytest.raises(BackupError, match="BACKUP_UNSAFE_MEMBER"):
        verify_backup(bad)


def test_source_symlink_and_corrupt_blob_rejected(source, tmp_path):
    db, docs, blob = source
    real = tmp_path / "blob"
    real.write_bytes(blob)
    path = docs / "doc-fixture.txt"
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(BackupError, match="BACKUP_DOCUMENT_UNREADABLE"):
        backup_database(db, docs, tmp_path / "symlink-source.pfb")
    path.unlink()
    path.write_bytes(b"tampered")
    with pytest.raises(BackupError, match="BACKUP_DOCUMENT_HASH_MISMATCH"):
        backup_database(db, docs, tmp_path / "tampered-source.pfb")


def test_missing_blob_rejected(source, bundle, tmp_path):
    bad = _rewrite(bundle, tmp_path / "missing.pfb", omit=["documents/doc-fixture.txt"])
    with pytest.raises(BackupError, match="BACKUP_DOCUMENT_SET_MISMATCH"):
        restore_backup(bad, tmp_path / "target")
    assert not (tmp_path / "target").exists()
    (source[1] / "doc-fixture.txt").unlink()
    with pytest.raises(BackupError, match="BACKUP_DOCUMENT_UNREADABLE"):
        backup_database(source[0], source[1], tmp_path / "missing-source.pfb")


@pytest.mark.parametrize("mutation,code", [
    (lambda m: m.update(version=99), "BACKUP_UNSUPPORTED_VERSION"),
    (lambda m: m["source"].update(fingerprint="a" * 64), "BACKUP_SOURCE_VERSION_MISMATCH"),
    (lambda m: m.update(schema_heads=["unknown"]), "BACKUP_SCHEMA_VERSION_MISMATCH"),
    (lambda m: m["database"].update(sha256="0" * 64), "BACKUP_DATABASE_HASH_MISMATCH"),
    (lambda m: m["documents"]["doc-fixture.txt"].update(sha256="0" * 64), "BACKUP_DOCUMENT_HASH_MISMATCH"),
    (lambda m: m["limits"].update(archive_bytes=2**60), "BACKUP_LIMITS_MISMATCH"),
])
def test_manifest_fail_closed_before_target(bundle, tmp_path, mutation, code):
    bad = _rewrite(bundle, tmp_path / "manifest.pfb", mutate_manifest=mutation)
    with pytest.raises(BackupError, match=code):
        restore_backup(bad, tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d["tables"].update(arbitrary_sql=[{"sql": "DROP TABLE x"}]), "BACKUP_SCHEMA_TABLES_MISMATCH"),
    (lambda d: d["tables"]["procurement_requests"][0].update(version=True), "BACKUP_INVALID_INTEGER"),
    (lambda d: d["tables"]["procurement_requests"][0].update(owner_id=None), "BACKUP_NULL_REQUIRED_VALUE"),
    (lambda d: d["tables"]["procurement_requests"][0].update(owner_id="x" * 81), "BACKUP_INVALID_STRING"),
    (lambda d: d["tables"]["documents"][0].update(request_id="missing"), "BACKUP_DATABASE_CONSTRAINTS"),
    (lambda d: d["tables"]["system_state"].clear(), "BACKUP_INVALID_RECOVERY_STATE"),
    (lambda d: d["tables"]["documents"][0].update(storage_key="../secret"), "BACKUP_UNSAFE_STORAGE_KEY"),
])
def test_rehashed_but_invalid_logical_data_rejected(bundle, tmp_path, mutation, code):
    bad = _rewrite(bundle, tmp_path / "bad-data.pfb", mutate_database=mutation)
    with pytest.raises(BackupError, match=code):
        restore_backup(bad, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_duplicate_json_keys_rejected(bundle, tmp_path):
    bad = _rewrite(bundle, tmp_path / "duplicate-json.pfb", raw_database=b'{"tables":{},"tables":{}}')
    with pytest.raises(BackupError, match="BACKUP_DUPLICATE_JSON_KEY"):
        verify_backup(bad)


def test_compressed_archive_rejected(bundle, tmp_path):
    bad = tmp_path / "compressed.pfb"
    with zipfile.ZipFile(bundle) as source_zip, zipfile.ZipFile(bad, "w", compression=zipfile.ZIP_DEFLATED) as destination:
        for member in source_zip.infolist():
            destination.writestr(member.filename, source_zip.read(member.filename))
    with pytest.raises(BackupError, match="BACKUP_UNSAFE_MEMBER"):
        verify_backup(bad)


def test_limits_checked_before_archive_read(bundle, tmp_path, monkeypatch):
    monkeypatch.setitem(LIMITS, "archive_bytes", 8)
    with pytest.raises(BackupError, match="BACKUP_ARCHIVE_LIMIT"):
        restore_backup(bundle, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_unknown_live_schema_table_rejected(source, tmp_path):
    db, docs, _ = source
    with db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE unknown_table (id INTEGER)"))
    with pytest.raises(BackupError, match="BACKUP_SCHEMA_TABLES_MISMATCH"):
        backup_database(db, docs, tmp_path / "unknown.pfb")


def test_unmigrated_source_rejected(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'unmigrated.sqlite3'}", create_schema=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    pause_writes(db)
    try:
        with pytest.raises(BackupError, match="BACKUP_SCHEMA_TABLES_MISMATCH"):
            backup_database(db, docs, tmp_path / "unmigrated.pfb")
    finally:
        db.engine.dispose()


def test_failed_restore_retains_incomplete_marker_without_purging(bundle, tmp_path, monkeypatch):
    import procureflow.backup as module
    target = tmp_path / "failed-target"
    sibling = tmp_path / "safe"
    sibling.write_text("keep")
    def fail(_, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(module, "_migrate", fail)
    with pytest.raises(BackupError, match="RESTORE_FAILED"):
        restore_backup(bundle, target)
    assert (target / ".restore-incomplete").exists() and sibling.read_text() == "keep"
    assert (target / "documents" / "doc-fixture.txt").exists()


def test_cli_safe_output_and_restore_ignores_ambient_database(bundle, tmp_path, capsys, monkeypatch):
    poison = tmp_path / "DO_NOT_TOUCH.sqlite3"
    monkeypatch.setenv("PF_DATABASE_URL", f"sqlite:///{poison}")
    monkeypatch.setenv("ERP_API_SECRET", "secret-must-not-be-copied")
    assert main(["verify", str(bundle), "--protection", "plain"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "verified"
    target = tmp_path / "cli-target"
    assert main(["restore", str(bundle), "--data-dir", str(target), "--protection", "plain"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["state"] == "RECOVERY"
    assert not poison.exists()
    assert "secret-must-not-be-copied" not in output
    assert not (target / ".env").exists()


@pytest.mark.postgres
def test_postgres_source_and_isolated_restore(pg_database, tmp_path):
    """The fixture requires *_test and opt-in. Only our fresh random schema is dropped."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    db = pg_database
    docs = tmp_path / "pg-documents"
    _seed(db, docs)
    pause_writes(db)
    bundle = tmp_path / "postgres.pfb"
    backup_database(db, docs, bundle)
    original = verify_backup(bundle)
    parsed = make_url(db.engine.url.render_as_string(hide_password=False))
    result, restored = None, None
    admin = create_engine(parsed, hide_parameters=True)
    try:
        result = restore_backup(bundle, tmp_path / "postgres-restored", postgres_url=parsed.render_as_string(hide_password=False))
        assert result.schema.startswith("pf_restore_")
        assert result.schema not in parsed.query.get("options", "")
        target_url = parsed.update_query_dict({"options": f"-csearch_path={result.schema}"})
        restored = Database(target_url.render_as_string(hide_password=False))
        assert restored.check_ready()["schema"] == "current"
        assert recovery_report(restored)["state"] == "RECOVERY"
        with restored.transaction() as session:
            actual = [dict(row) for row in session.execute(select(OperationRow.__table__).order_by(OperationRow.id)).mappings()]
            assert actual == original.tables["external_operations"]
        # Sequence advancement is verified by a new operator audit row.
        with restored.operator_transaction() as session:
            receipt = EventRow(tenant_id="synthetic", actor_id="operator", type="TEST_RESTORE", payload={})
            session.add(receipt)
            session.flush()
            assert receipt.id > 50
        assert recovery_report(db)["state"] == "PAUSED"
    finally:
        if restored is not None:
            restored.engine.dispose()
        if result is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{result.schema}" CASCADE'))
        admin.dispose()


def test_explicit_url_file_permissions_and_secret_safe_cli(source, tmp_path, capsys):
    from procureflow.backup_cli import _database_url_file
    credentials = tmp_path / "database-url"
    credentials.write_text(source[0].engine.url.render_as_string(hide_password=False) + "\n")
    credentials.chmod(0o644)
    with pytest.raises(BackupError, match="DATABASE_URL_FILE_MUST_BE_OWNER_ONLY"):
        _database_url_file(credentials)
    credentials.chmod(0o600)
    assert main(["backup", "--protection", "plain", "--database-url-file", str(credentials), "--documents", str(source[1]),
                 "--output", str(tmp_path / "cli-backup.pfb")]) == 0
    output = capsys.readouterr()
    assert "sqlite:///" not in output.out + output.err
    alias = tmp_path / "secret-link"
    alias.symlink_to(credentials)
    assert main(["backup", "--protection", "plain", "--database-url-file", str(alias), "--documents", str(source[1]),
                 "--output", str(tmp_path / "rejected-cli.pfb")]) == 2
    assert "sqlite:///" not in capsys.readouterr().err


def test_backup_excludes_unreferenced_files_and_configuration(source, tmp_path):
    (source[1] / "orphan.txt").write_text("Not referenced")
    (source[1] / ".env").write_text("SYNTHETIC_SECRET=do-not-archive")
    output = tmp_path / "referenced-only.pfb"
    backup_database(source[0], source[1], output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"manifest.json", "database.json", "documents/doc-fixture.txt"}
        assert all(b"do-not-archive" not in archive.read(name) for name in archive.namelist())


@pytest.mark.parametrize("limit,value", [("document_bytes", 1), ("members", 2), ("database_bytes", 1), ("manifest_bytes", 1)])
def test_member_resource_limits_checked_before_read(bundle, monkeypatch, limit, value):
    monkeypatch.setitem(LIMITS, limit, value)
    with pytest.raises(BackupError, match="BACKUP_ARCHIVE_LIMIT"):
        verify_backup(bundle)


def test_excessive_json_depth_rejected(bundle, tmp_path):
    nested = {}
    for _ in range(LIMITS["json_depth"] + 1):
        nested = {"nested": nested}
    bad = _rewrite(bundle, tmp_path / "deep.pfb", mutate_database=lambda d: d["tables"]["procurement_requests"][0].update(data=nested))
    with pytest.raises(BackupError, match="BACKUP_JSON_DEPTH_LIMIT"):
        verify_backup(bad)


def test_recovery_state_commits_with_schema_before_import(tmp_path):
    target = tmp_path / "schema-only.sqlite3"
    db = Database(f"sqlite:///{target}")
    try:
        _migrate(db, recovery_id="a" * 32)
        assert db.state()["state"] == "RECOVERY"
        assert db.state()["required_auth_mode"] == "pilot"
    finally:
        db.engine.dispose()


def test_invalid_postgres_url_never_creates_target(bundle, tmp_path):
    target = tmp_path / "wrong-postgres"
    with pytest.raises(BackupError, match="RESTORE_POSTGRES_URL_REQUIRED"):
        restore_backup(bundle, target, postgres_url="sqlite:///:memory:")
    assert not target.exists()


def test_previously_revoked_credentials_keep_original_timestamp(source, tmp_path):
    db, docs, _ = source
    with db.operator_transaction() as session:
        session.execute(Base.metadata.tables["pilot_sessions"].update().values(revoked_at=STAMP))
        session.execute(Base.metadata.tables["pilot_invites"].update().values(revoked_at=STAMP))
    archive = tmp_path / "revoked.pfb"
    backup_database(db, docs, archive)
    result = restore_backup(archive, tmp_path / "revoked-target")
    restored = Database(f"sqlite:///{result.data_dir / 'procureflow.sqlite3'}")
    try:
        with restored.transaction() as session:
            for name in ("pilot_invites", "pilot_sessions"):
                assert session.execute(select(Base.metadata.tables[name].c.revoked_at)).scalar() == STAMP
    finally:
        restored.engine.dispose()


@pytest.mark.parametrize("lying_count", [False, True])
def test_central_directory_preflight_rejects_many_members_before_zipfile(tmp_path, monkeypatch, lying_count):
    """Build a 10003-member classic ZIP without allocating ZipInfo objects."""
    import struct
    import procureflow.backup as module
    path = tmp_path / "many-members.pfb"
    name = b"empty"
    count = LIMITS["members"] + 1
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0) + name
    with path.open("wb") as stream:
        for _ in range(count):
            stream.write(local)
        directory_offset = stream.tell()
        for index in range(count):
            stream.write(struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0,
                                     len(name), 0, 0, 0, 0, 0, index * len(local)) + name)
        directory_bytes = stream.tell() - directory_offset
        advertised = 2 if lying_count else count
        stream.write(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, advertised, advertised,
                                 directory_bytes, directory_offset, 0))
    def forbidden(*args, **kwargs):
        raise AssertionError("ZipFile must not see an unbounded central directory")
    monkeypatch.setattr(module.zipfile, "ZipFile", forbidden)
    target = tmp_path / "untouched"
    with pytest.raises(BackupError, match="BACKUP_ARCHIVE_(INVALID|LIMIT)"):
        restore_backup(path, target)
    assert not target.exists()


@pytest.mark.parametrize("addition", [b"prefix", b"trailing"])
def test_zip_prefix_and_trailing_data_rejected(bundle, tmp_path, addition):
    raw = bundle.read_bytes()
    path = tmp_path / "polyglot.pfb"
    path.write_bytes(addition + raw if addition == b"prefix" else raw + addition)
    with pytest.raises(BackupError, match="BACKUP_ARCHIVE_INVALID"):
        verify_backup(path)


def test_json_allocation_budget_precedes_parser(monkeypatch):
    import procureflow.backup as module
    content = b"[" + b"0," * LIMITS["json_tokens"] + b"0]"
    def forbidden(*args, **kwargs):
        raise AssertionError("JSON parser must not allocate an unbounded object tree")
    monkeypatch.setattr(module.json, "loads", forbidden)
    with pytest.raises(BackupError, match="BACKUP_JSON_TOKEN_LIMIT"):
        module._load_json(content)


def test_json_huge_integer_has_controlled_error():
    from procureflow.backup import _load_json
    with pytest.raises(BackupError, match="BACKUP_INVALID_JSON"):
        _load_json(b'{"number":' + b"1" * 5000 + b"}")


def test_incomplete_source_cannot_be_backed_up(source, tmp_path):
    (source[1].parent / ".restore-incomplete").write_text("incomplete")
    output = tmp_path / "incomplete.pfb"
    with pytest.raises(BackupError, match="BACKUP_INCOMPLETE_SOURCE"):
        backup_database(source[0], source[1], output)
    assert not output.exists()


def test_backup_never_emits_bundle_exceeding_reader_json_limits(source, tmp_path, monkeypatch):
    monkeypatch.setitem(LIMITS, "json_tokens", 100)
    output = tmp_path / "over-budget.pfb"
    with pytest.raises(BackupError, match="BACKUP_JSON_TOKEN_LIMIT"):
        backup_database(source[0], source[1], output)
    assert not output.exists()
