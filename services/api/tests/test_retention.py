"""Synthetic-only retention proofs: no deleted files, no changed business evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import stat

import pytest
from sqlalchemy import select

from conftest import BUYER, OTHER, request
from procureflow.db import Base, Database, DocumentRow, EventRow, RequestRow, TableImportRow, uid
from procureflow.errors import DomainError
from procureflow.retention import RetentionService, _hash, pending_import_count
from test_table_imports import confirm_import, map_table, table_bytes, upload_table


def expire(service, import_id, hours=48):
    with service.db.transaction(write=True) as session:
        session.get(TableImportRow, import_id).expires_at = (
            datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def row_state(db, import_id):
    with db.transaction() as session:
        row = session.get(TableImportRow, import_id)
        return {c.name: deepcopy(getattr(row, c.name)) for c in TableImportRow.__table__.columns}


def seed_pending(service, request_id, count, *, expired=False, tenant="demo"):
    # Seed only synthetic preview rows and synthetic source bytes; not production fixtures.
    with service.db.transaction(write=True) as session:
        for index in range(count):
            identity = uid("tim_")
            data = f"synthetic source {identity} {index}".encode()
            (service.document_dir / (identity + ".csv")).write_bytes(data)
            session.add(TableImportRow(id=identity, tenant_id=tenant, request_id=request_id,
                filename="synthetic.csv", sha256=hashlib.sha256(data).hexdigest(), storage_key=identity + ".csv",
                table={"sheets": [], "issues": []}, revision=1, request_version=1, status="OPEN",
                expires_at=(datetime.now(timezone.utc) + timedelta(hours=-48 if expired else 1)).isoformat()))


def snapshot_business(db):
    with db.transaction() as session:
        return {table.name: [dict(row) for row in session.execute(select(table)).mappings()]
                for table in Base.metadata.sorted_tables if table.name not in {"table_imports", "audit_events"}}


def test_expired_previews_no_longer_exhaust_active_quota(system):
    client, service, _ = system
    req = request(client)
    seed_pending(service, req["id"], 100, expired=True)
    draft = upload_table(client, req["id"])
    with service.db.transaction() as session:
        assert pending_import_count(session, "demo") == 1
        assert len(list(session.scalars(select(TableImportRow)))) == 101
    assert draft["status"] == "OPEN"


def test_active_limit_applies_to_new_upload_and_expired_reopen(system):
    client, service, _ = system
    req = request(client)
    old = upload_table(client, req["id"])
    expire(service, old["id"])
    seed_pending(service, req["id"], 100)
    original = row_state(service.db, old["id"])
    files = sorted(service.document_dir.iterdir())
    for content in (table_bytes(), table_bytes(supplier_id="SUP-NEW")):
        result = client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
                             files={"file": ("source.csv", content)})
        assert result.status_code == 413 and result.json()["error"]["code"] == "TABLE_IMPORT_LIMIT"
    assert row_state(service.db, old["id"]) == original
    assert sorted(service.document_dir.iterdir()) == files


def test_plan_archive_replay_restart_unarchive_preserves_bytes_and_evidence(system):
    client, service, erp = system
    req = request(client)
    mapped = map_table(client, upload_table(client, req["id"]))
    expire(service, mapped["id"])
    before = row_state(service.db, mapped["id"])
    business = snapshot_business(service.db)
    blobs = {path.name: path.read_bytes() for path in service.document_dir.iterdir()}
    retention = RetentionService(service.db)
    plan = retention.plan("demo")
    assert [entry["import_id"] for entry in plan["entries"]] == [mapped["id"]]
    assert row_state(service.db, mapped["id"]) == before  # Dry run is a read.
    result = retention.apply(plan, tenant_id="demo", actor="operator-01")
    assert result["changed"] == [mapped["id"]] and result["files_deleted"] == 0
    after = row_state(service.db, mapped["id"])
    assert after == {**before, "status": "ARCHIVED", "revision": before["revision"] + 1}
    assert snapshot_business(service.db) == business
    assert {path.name: path.read_bytes() for path in service.document_dir.iterdir()} == blobs
    restarted = Database(service.settings.database_url)
    try:
        replay = RetentionService(restarted).apply(plan, tenant_id="demo", actor="operator-02")
        assert replay["changed"] == [] and replay["already_applied"] == [mapped["id"]]
    finally:
        restarted.engine.dispose()
    assert upload_table(client, req["id"])["status"] == "ARCHIVED"  # No implicit revival.
    view = client.get(f"/api/v1/table-imports/{mapped['id']}", headers=BUYER).json()
    assert view["can_confirm"] is False and view["values"] == mapped["values"]
    response = client.post(f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
                          json={"expected_revision": after["revision"], "acknowledge": True})
    assert response.status_code == 410 and response.json()["error"]["code"] == "TABLE_IMPORT_ARCHIVED"
    reverse = retention.plan("demo", operation="unarchive", import_ids=[mapped["id"]])
    assert retention.apply(reverse, tenant_id="demo", actor="operator-01")["changed"] == [mapped["id"]]
    reopened = row_state(service.db, mapped["id"])
    assert reopened == {**before, "revision": before["revision"] + 2}
    response = client.post(f"/api/v1/table-imports/{mapped['id']}/confirm", headers=BUYER,
                          json={"expected_revision": reopened["revision"], "acknowledge": True})
    assert response.status_code == 410 and response.json()["error"]["code"] == "TABLE_IMPORT_EXPIRED"
    with pytest.raises(DomainError, match="changed"):
        retention.apply(plan, tenant_id="demo", actor="operator-01")
    refreshed = upload_table(client, req["id"])
    assert refreshed["selection"] is None and not refreshed["can_confirm"]
    assert refreshed["revision"] == reopened["revision"] + 1
    with service.db.transaction() as session:
        events = list(session.scalars(select(EventRow).where(EventRow.type.in_(
            ["TABLE_IMPORT_ARCHIVED", "TABLE_IMPORT_UNARCHIVED"]))))
        assert len(events) == 2 and all(event.actor_id == "operator-01" for event in events)
    assert erp.count() == 0


def test_imported_and_shared_source_references_are_never_archived(system):
    client, service, _ = system
    req = request(client)
    imported = map_table(client, upload_table(client, req["id"]))
    quote = confirm_import(client, imported)
    expire(service, imported["id"])
    shared = upload_table(client, req["id"], table_bytes(supplier_id="SUP-B"))
    expire(service, shared["id"])
    with service.db.transaction(write=True) as session:
        a, b = session.get(TableImportRow, imported["id"]), session.get(TableImportRow, shared["id"])
        b.storage_key = a.storage_key
    before = snapshot_business(service.db)
    imported_before = row_state(service.db, imported["id"])
    shared_before = row_state(service.db, shared["id"])
    plan = RetentionService(service.db).plan("demo")
    assert plan["entries"] == []
    RetentionService(service.db).apply(plan, tenant_id="demo", actor="operator")
    assert row_state(service.db, imported["id"]) == imported_before
    assert row_state(service.db, shared["id"]) == shared_before
    assert snapshot_business(service.db) == before
    assert confirm_import(client, imported)["id"] == quote["id"]
    with service.db.transaction() as session:
        assert pending_import_count(session, "demo") == 1  # Protected OPEN corruption fails closed.


def test_same_request_source_hash_and_document_id_are_protected(system):
    client, service, _ = system
    req = request(client)
    a = upload_table(client, req["id"])
    expire(service, a["id"])
    with service.db.transaction(write=True) as session:
        row = session.get(TableImportRow, a["id"])
        session.add(DocumentRow(id="doc_" + row.id[4:], tenant_id="demo", request_id=req["id"],
            filename="synthetic", sha256=row.sha256, storage_key="another-key", fragments=[]))
    assert RetentionService(service.db).plan("demo")["entries"] == []


def test_tenant_isolation_and_explicit_tenant_match(system):
    client, service, _ = system
    req = request(client)
    mine = upload_table(client, req["id"])
    other_req = client.post("/api/v1/requests", headers=OTHER, json={"title": "Other", "sku": "STAND-01",
        "quantity": "20", "budget": "30000.00", "max_delivery_days": 14}).json()
    other = upload_table(client, other_req["id"], headers=OTHER)
    for item in (mine, other):
        expire(service, item["id"])
    retention = RetentionService(service.db)
    plan = retention.plan("demo")
    assert [entry["import_id"] for entry in plan["entries"]] == [mine["id"]]
    with pytest.raises(DomainError) as caught:
        retention.apply(plan, tenant_id="other", actor="operator")
    assert caught.value.code == "RETENTION_TENANT_MISMATCH"
    with pytest.raises(DomainError):
        retention.plan("demo", import_ids=[other["id"]])
    retention.apply(plan, tenant_id="demo", actor="operator")
    assert row_state(service.db, other["id"])["status"] == "OPEN"
    assert client.get(f"/api/v1/table-imports/{mine['id']}", headers=OTHER).status_code == 404


@pytest.mark.parametrize("mutation", ["reopen", "mapping", "reference", "expiry"])
def test_stale_plan_rolls_back_entire_batch(system, mutation):
    client, service, _ = system
    req = request(client)
    first = upload_table(client, req["id"])
    second = upload_table(client, req["id"], table_bytes(supplier_id="SUP-B"))
    for item in (first, second):
        expire(service, item["id"])
    retention = RetentionService(service.db)
    plan = retention.plan("demo")
    last = plan["entries"][-1]["import_id"]
    with service.db.transaction(write=True) as session:
        row = session.get(TableImportRow, last)
        if mutation == "reopen":
            row.revision += 1
            row.expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        elif mutation == "mapping":
            row.selection = {"changed": True}  # Even corruption without revision is detected.
        elif mutation == "expiry":
            row.expires_at = "invalid"
        else:
            session.add(DocumentRow(id=uid("doc_"), tenant_id="demo", request_id=req["id"],
                filename="linked-source", sha256=row.sha256, storage_key=row.storage_key, fragments=[]))
    with pytest.raises(DomainError) as caught:
        retention.apply(plan, tenant_id="demo", actor="operator")
    assert caught.value.code == "RETENTION_PLAN_STALE"
    assert all(row_state(service.db, item["id"])["status"] == "OPEN" for item in (first, second))
    with service.db.transaction() as session:
        assert not list(session.scalars(select(EventRow).where(EventRow.type == "TABLE_IMPORT_ARCHIVED")))


def test_plan_bounds_timestamp_validation_and_tampering(system):
    client, service, _ = system
    req = request(client)
    rows = [upload_table(client, req["id"], table_bytes(supplier_id=f"SUP-{index}")) for index in range(3)]
    for item in rows:
        expire(service, item["id"])
    retention = RetentionService(service.db)
    assert len(retention.plan("demo", limit=2)["entries"]) == 2
    for args in ({"retention_hours": 0}, {"retention_hours": 8761}, {"limit": 1001},
                 {"operation": "unarchive"}, {"limit": True}):
        with pytest.raises(DomainError):
            retention.plan("demo", **args)
    plan = retention.plan("demo")
    altered = deepcopy(plan)
    altered["entries"][0]["revision"] += 1
    with pytest.raises(DomainError) as caught:
        retention.apply(altered, tenant_id="demo", actor="operator")
    assert caught.value.code == "RETENTION_PLAN_INVALID"
    expired = deepcopy(plan)
    expired["generated_at"] = "2000-01-01T00:00:00+00:00"
    expired["valid_until"] = "2000-01-01T01:00:00+00:00"
    expired["plan_id"] = _hash({key: value for key, value in expired.items() if key != "plan_id"})
    with pytest.raises(DomainError) as caught:
        retention.apply(expired, tenant_id="demo", actor="operator")
    assert caught.value.code == "RETENTION_PLAN_EXPIRED"
    with service.db.transaction(write=True) as session:
        session.get(TableImportRow, rows[0]["id"]).expires_at = "2000-01-01T00:00:00"  # naive
        session.get(TableImportRow, rows[1]["id"]).expires_at = "not-a-date"
    assert len(retention.plan("demo")["entries"]) == 1
    with service.db.transaction() as session:
        assert pending_import_count(session, "demo") == 2


def test_recent_expiration_exits_quota_but_waits_retention_grace(system):
    client, service, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    expire(service, draft["id"], hours=1)
    assert RetentionService(service.db).plan("demo")["entries"] == []
    with service.db.transaction() as session:
        assert pending_import_count(session, "demo") == 0


def test_concurrent_archive_and_reupload_remain_serialized(system):
    client, service, _ = system
    req = request(client)
    draft = map_table(client, upload_table(client, req["id"]))
    expire(service, draft["id"])
    retention = RetentionService(service.db)
    plan = retention.plan("demo")

    def archive():
        try:
            return retention.apply(plan, tenant_id="demo", actor="operator")
        except DomainError as error:
            assert error.code == "RETENTION_PLAN_STALE"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_future = pool.submit(archive)
        upload_future = pool.submit(upload_table, client, req["id"])
        result, reupload = archive_future.result(), upload_future.result()
    row = row_state(service.db, draft["id"])
    if result:
        assert row["status"] == "ARCHIVED" and reupload["status"] == "ARCHIVED"
    else:
        assert row["status"] == "OPEN" and reupload["selection"] is None
    assert row["revision"] == draft["revision"] + 1
    with service.db.transaction() as session:
        assert list(session.scalars(select(DocumentRow))) == []


def test_concurrent_confirm_of_expired_preview_never_creates_quote(system):
    client, service, erp = system
    req = request(client)
    draft = map_table(client, upload_table(client, req["id"]))
    expire(service, draft["id"])
    retention = RetentionService(service.db)
    plan = retention.plan("demo")
    with ThreadPoolExecutor(max_workers=2) as pool:
        apply = pool.submit(retention.apply, plan, tenant_id="demo", actor="operator")
        confirm = pool.submit(client.post, f"/api/v1/table-imports/{draft['id']}/confirm", headers=BUYER,
                              json={"expected_revision": draft["revision"], "acknowledge": True})
        assert apply.result()["changed"] == [draft["id"]]
        response = confirm.result()
        assert response.status_code in {409, 410}
    assert erp.count() == 0
    with service.db.transaction() as session:
        assert not list(session.scalars(select(DocumentRow)))


def test_cli_defaults_dry_run_and_requires_explicit_mutation(system, monkeypatch, tmp_path, capsys):
    from procureflow.retention_cli import main

    client, service, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    expire(service, draft["id"])
    monkeypatch.setenv("PF_DATABASE_URL", service.settings.database_url)
    monkeypatch.setenv("PF_DATA_DIR", str(service.settings.data_dir))
    plan_path = tmp_path / "plan.json"
    assert main(["plan", "--tenant", "demo", "--output", str(plan_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] and report["entries"] == 1
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert row_state(service.db, draft["id"])["status"] == "OPEN"
    args = ["apply", "--tenant", "demo", "--actor", "operator", "--plan", str(plan_path)]
    assert main(args) == 2
    assert row_state(service.db, draft["id"])["status"] == "OPEN"
    assert main([*args, "--apply"]) == 0
    assert row_state(service.db, draft["id"])["status"] == "ARCHIVED"
    assert main([*args, "--apply"]) == 0
    assert main(["plan", "--tenant", "demo", "--output", str(plan_path)]) == 1  # Never overwrite.


def test_archive_leaves_full_procurement_receipts_and_existing_audit_unchanged(system):
    from conftest import approved, enqueue

    client, service, erp = system
    approved_request, _, proposal = approved(client)
    operation = enqueue(client, approved_request, proposal)
    response = client.post(f"/api/v1/operations/{operation['id']}/process", headers=BUYER)
    assert response.status_code == 200 and response.json()["status"] == "COMPLETED"
    req = request(client)
    draft = map_table(client, upload_table(client, req["id"]))
    expire(service, draft["id"])
    before = snapshot_business(service.db)
    with service.db.transaction() as session:
        events = [dict(row) for row in session.execute(select(EventRow.__table__)).mappings()]
    retention = RetentionService(service.db)
    retention.apply(retention.plan("demo"), tenant_id="demo", actor="operator")
    assert snapshot_business(service.db) == before
    with service.db.transaction() as session:
        after_events = [dict(row) for row in session.execute(select(EventRow.__table__)).mappings()]
    assert after_events[:-1] == events and after_events[-1]["type"] == "TABLE_IMPORT_ARCHIVED"
    assert erp.count() == 1


def test_concurrent_new_uploads_share_tenant_last_slot(system):
    client, service, _ = system
    requests = [request(client), request(client)]
    seed_pending(service, requests[0]["id"], 99)

    def upload_one(req):
        return client.post(f"/api/v1/requests/{req['id']}/table-imports", headers=BUYER,
                           files={"file": ("source.csv", table_bytes())})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload_one, requests))
    assert sorted(result.status_code for result in results) == [201, 413]
    with service.db.transaction() as session:
        assert pending_import_count(session, "demo") == 100


def test_maintenance_pause_allows_plan_but_blocks_archive_mutation(system):
    from procureflow.maintenance import pause_writes

    client, service, _ = system
    req = request(client)
    draft = upload_table(client, req["id"])
    expire(service, draft["id"])
    before = row_state(service.db, draft["id"])
    pause_writes(service.db)
    retention = RetentionService(service.db)
    plan = retention.plan("demo")
    assert len(plan["entries"]) == 1
    with pytest.raises(DomainError) as caught:
        retention.apply(plan, tenant_id="demo", actor="operator")
    assert caught.value.code == "WRITES_PAUSED"
    assert row_state(service.db, draft["id"]) == before
