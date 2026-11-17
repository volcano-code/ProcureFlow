"""Synthetic held operations: read-only diagnostics cannot turn evidence into authority."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from conftest import APPROVER, AUDITOR, BUYER, OTHER, approved, enqueue
from procureflow.db import (ApprovalRow, Base, DocumentRow, OperationRow, OutboxRow,
                            RecoveryHoldRow, SystemStateRow, now)
from procureflow.domain import digest
from procureflow.erp import ERPRejected, ERPUnknown
from procureflow.maintenance import pause_writes, recovery_report, resume_writes
from procureflow.recovery import offline_recovery_detail


def held(system):
    client, service, erp = system
    request, quote, proposal = approved(client)
    operation = enqueue(client, request, proposal)
    with service.db.transaction(write=True) as session:
        session.add(RecoveryHoldRow(operation_id=operation["id"], restore_id="synthetic-restore",
            original_status="IN_FLIGHT", created_at=now()))
    return operation["id"], proposal


def snapshot(db):
    with db.transaction() as session:
        return {table.name: [dict(row) for row in session.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                for table in Base.metadata.sorted_tables}


def get(client, path, headers=AUDITOR):
    response = client.get("/api/v1/recovery/operations" + path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_inventory_details_and_all_reader_roles_leave_entire_database_unchanged(system):
    client, service, erp = system
    oid, proposal = held(system)
    before = snapshot(service.db)
    for headers in (BUYER, APPROVER, AUDITOR):
        listing = get(client, "", headers)
        assert listing["total"] == 1 and listing["next_after"] is None
        assert listing["items"][0]["id"] == oid
        detail = get(client, "/" + oid, headers)
        assert detail["hold"]["original_status"] == "IN_FLIGHT"
        assert detail["expected"]["total"] == proposal["total"]
        assert detail["approval"]["authority_current"] is True
        assert detail["sources"][0]["integrity"] == "verified"
        assert detail["replay_permitted"] is False
        assert "RECOVERY_HOLD_PERMANENT" in detail["diagnostics"]
        receipt = get(client, "/" + oid + "/reconciliation", headers)
        assert receipt["status"] == "missing"
        assert receipt["reason"] == "REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT"
        assert receipt["external_write_attempted"] is False
        assert receipt["replay_permitted"] is False
    assert snapshot(service.db) == before
    assert erp.count() == 0


def test_tenant_auth_and_held_only_filters_before_any_erp_read(system, monkeypatch):
    client, service, erp = system
    oid, _ = held(system)
    request, _, proposal = approved(client)
    unheld = enqueue(client, request, proposal)["id"]
    def forbidden(*args):
        raise AssertionError("ERP read must not happen before authorization")
    monkeypatch.setattr(erp, "find", forbidden)
    assert get(client, "", OTHER)["items"] == []
    for target in (oid, unheld):
        for suffix in ("", "/reconciliation"):
            assert client.get(f"/api/v1/recovery/operations/{target}{suffix}", headers=OTHER).status_code == 404
            assert client.get(f"/api/v1/recovery/operations/{target}{suffix}").status_code == 401
    assert client.get(f"/api/v1/recovery/operations/{unheld}", headers=AUDITOR).status_code == 404


def test_cursor_pagination_bounds_and_total_are_tenant_scoped(system):
    client, _, _ = system
    identifiers = sorted(held(system)[0] for _ in range(3))
    cursor, seen = "", []
    for _ in range(3):
        page = get(client, "?limit=1" + ("&after=" + cursor if cursor else ""))
        assert page["total"] == 3
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_after"]
    assert seen == identifiers and cursor is None
    for query in ("?limit=0", "?limit=101", "?after=not-an-id"):
        assert client.get("/api/v1/recovery/operations" + query, headers=AUDITOR).status_code == 422


def test_matching_lost_response_never_completes_or_replays_held_operation(system, monkeypatch):
    client, service, erp = system
    oid, payload = held(system)
    erp.create_draft(oid, payload)  # External commit existed before local restore.
    before = snapshot(service.db)
    def no_write(*args):
        pytest.fail("Recovery diagnostics attempted an ERP write")
    monkeypatch.setattr(erp, "create_draft", no_write)
    monkeypatch.setattr(erp, "create_draft_guarded", no_write)
    for _ in range(2):
        receipt = get(client, "/" + oid + "/reconciliation")
        assert receipt["status"] == "verified" and receipt["draft_verified"] is True
        assert receipt["differences"] == [] and receipt["replay_permitted"] is False
    response = client.post(f"/api/v1/operations/{oid}/process", headers=BUYER)
    assert response.status_code == 409 and response.json()["error"]["code"] == "RECOVERY_OPERATION_HELD"
    assert snapshot(service.db) == before and erp.count() == 1


@pytest.mark.parametrize("field,value", [("quantity", "21"), ("unit_price", "999"), ("total", "1.00"),
    ("supplier_id", "other"), ("sku", "other"), ("currency", "USD"), ("uom", "BOX"),
    ("company", "other"), ("transaction_date", "2000-01-01"), ("snapshot_hash", "0" * 64),
    ("operation_key", "0" * 64), ("docstatus", 1), ("docstatus", False), ("simulated", False)])
def test_field_drift_reports_actual_difference_without_writing(system, monkeypatch, field, value):
    client, service, erp = system
    oid, payload = held(system)
    remote = erp.create_draft(oid, payload)
    remote[field] = value
    monkeypatch.setattr(erp, "find", lambda _: remote)
    before = snapshot(service.db)
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] == "mismatch"
    assert field in {d["field"] for d in result["differences"]}
    assert not result["matches_snapshot"] and not result["replay_permitted"]
    assert snapshot(service.db) == before


def test_cost_components_and_remote_id_drift_even_with_same_total(system, monkeypatch):
    client, service, erp = system
    oid, payload = held(system)
    remote = erp.create_draft(oid, payload)
    with service.db.transaction(write=True) as session:
        session.get(OperationRow, oid).remote_id = "ORIGINAL-ID"
    remote["cost_values"]["shipping_cost"] = "999.00"
    remote["unexpected_remote_secret"] = "secret-value"
    monkeypatch.setattr(erp, "find", lambda _: remote)
    result = get(client, "/" + oid + "/reconciliation")
    assert {"name", "cost_values.shipping_cost"} <= {d["field"] for d in result["differences"]}
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize("failure,status,reason", [
    (ERPUnknown("password=SECRET upstream URL"), "unavailable", "ERP_VERIFICATION_UNAVAILABLE"),
    (TimeoutError("SECRET"), "unavailable", "ERP_VERIFICATION_UNAVAILABLE"),
    (ERPRejected("ERP_REMOTE_KEY_NOT_UNIQUE SECRET"), "mismatch", "ERP_DOCUMENT_REJECTED"),
])
def test_upstream_failures_do_not_leak_or_mutate(system, monkeypatch, failure, status, reason):
    client, service, erp = system
    oid, _ = held(system)
    before = snapshot(service.db)
    def fail(_):
        raise failure
    monkeypatch.setattr(erp, "find", fail)
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] == status and result["reason"] == reason
    assert "SECRET" not in json.dumps(result)
    assert snapshot(service.db) == before


@pytest.mark.parametrize("remote", [[], "unexpected", False, {"name": {"secret": "NEVER_SHOW"}}])
def test_malformed_response_is_not_a_verified_receipt(system, monkeypatch, remote):
    client, _, erp = system
    oid, _ = held(system)
    monkeypatch.setattr(erp, "find", lambda _: remote)
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] in {"unavailable", "mismatch"}
    assert not result["matches_snapshot"] and "NEVER_SHOW" not in json.dumps(result)


@pytest.mark.parametrize("change", ["payload", "hash", "target"])
def test_invalid_local_binding_blocks_before_network(system, monkeypatch, change):
    client, service, erp = system
    oid, _ = held(system)
    with service.db.transaction(write=True) as session:
        op = session.get(OperationRow, oid)
        if change == "payload":
            op.payload = {**op.payload, "total": "1"}
        elif change == "hash":
            op.snapshot_hash = "0" * 64
        else:
            payload = {**op.payload, "erp_target_fingerprint": "0" * 64}
            payload["snapshot_hash"] = digest({k: v for k, v in payload.items() if k != "snapshot_hash"})
            op.payload, op.snapshot_hash = payload, payload["snapshot_hash"]
    calls = []
    monkeypatch.setattr(erp, "find", lambda key: calls.append(key))
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] == "blocked" and calls == []


@pytest.mark.parametrize("fault,expected", [("missing", "missing"), ("corrupt", "mismatch"),
    ("symlink", "unavailable"), ("traversal", "unavailable"), ("oversized", "unavailable")])
def test_source_faults_are_per_file_diagnostics(system, tmp_path, fault, expected):
    client, service, _ = system
    oid, _ = held(system)
    with service.db.transaction(write=True) as session:
        document = session.scalar(select(DocumentRow))
        path = service.document_dir / document.storage_key
        if fault == "missing":
            path.unlink()
        elif fault == "corrupt":
            path.write_bytes(b"Corruption")
        elif fault == "symlink":
            path.unlink()
            secret = tmp_path / "secret"
            secret.write_text("NEVER_SHOW")
            path.symlink_to(secret)
        elif fault == "traversal":
            document.storage_key = "../secret"
        elif fault == "oversized":
            with path.open("wb") as stream:
                stream.truncate(16 * 1024 * 1024 + 1)
    result = get(client, "/" + oid)
    assert result["sources"][0]["integrity"] == expected
    assert "NEVER_SHOW" not in json.dumps(result)


def test_paused_gets_and_offline_inspection_do_not_unlock_writes(system):
    client, service, erp = system
    oid, _ = held(system)
    pause_writes(service.db)
    before = snapshot(service.db)
    assert get(client, "")["state"]["state"] == "PAUSED"
    assert get(client, "/" + oid + "/reconciliation")["status"] == "missing"
    detail = offline_recovery_detail(service.db, service.document_dir, "demo", oid)
    assert detail["approval"]["authority_evaluated"] is False
    assert client.post(f"/api/v1/operations/{oid}/process", headers=BUYER).status_code == 503
    assert snapshot(service.db) == before
    report = recovery_report(service.db)
    resume_writes(service.db, generation=report["generation"], ledger_sha256=report["ledger_sha256"])
    assert client.post(f"/api/v1/operations/{oid}/process", headers=BUYER).status_code == 409
    assert erp.count() == 0


def test_concurrent_local_ledger_change_blocks_observation(system, monkeypatch):
    client, service, erp = system
    oid, payload = held(system)
    remote = erp.create_draft(oid, payload)
    def find(_):
        with service.db.transaction(write=True) as session:
            session.get(OperationRow, oid).remote_id = "CONCURRENT-ID"
        return remote
    monkeypatch.setattr(erp, "find", find)
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] == "blocked" and result["reason"] == "LOCAL_LEDGER_CHANGED_DURING_READ"
    assert not result["matches_snapshot"] and not result["draft_verified"]


def test_no_mutation_routes_are_exposed(system):
    client, _, _ = system
    oid, _ = held(system)
    for suffix in ("/release", "/resume", "/acknowledge", "/resolve", "/reconciliation"):
        assert client.post("/api/v1/recovery/operations/" + oid + suffix, headers=APPROVER).status_code in {404, 405}


def pilot_reader(system):
    """Fresh controlled identities on disposable synthetic data, not restored auth."""
    from fastapi.testclient import TestClient
    from procureflow.app import create_app
    from procureflow.auth import IdentityService
    client, service, erp = system
    oid, payload = held(system)
    settings = replace(service.settings, mode="pilot", auth_tokens={})
    identities = IdentityService(service.db, settings)
    identities.create_tenant("demo")
    for user, role in (("buyer-01", "buyer"), ("approver-01", "approver"), ("auditor-01", "auditor")):
        identities.set_membership("demo", user, role)
    app = create_app(settings, database=service.db, erp=erp)
    client = TestClient(app)
    credential = identities.issue_invite("demo", "auditor-01")["credential"]
    result = client.post("/api/v1/auth/login", json={"credential": credential})
    assert result.status_code == 200
    headers = {"Authorization": "Bearer " + result.json()["token"]}
    return client, app.state.service, identities, headers, oid, payload


@pytest.mark.parametrize("change", ["revoke", "expire", "role", "tenant"])
def test_reader_revocation_during_remote_read_returns_no_protected_result(system, monkeypatch, change):
    from procureflow.db import PilotSessionRow
    client, service, identities, headers, oid, payload = pilot_reader(system)
    remote = system[2].create_draft(oid, payload)
    def find(_):
        if change == "revoke":
            identities.set_membership("demo", "auditor-01", "auditor", active=False)
        elif change == "role":
            identities.set_membership("demo", "auditor-01", "buyer")
        elif change == "tenant":
            identities.set_tenant_active("demo", False)
        else:
            with service.db.transaction(write=True) as session:
                session.scalar(select(PilotSessionRow)).expires_at = "2000-01-01T00:00:00+00:00"
        return remote
    monkeypatch.setattr(system[2], "find", find)
    result = client.get(f"/api/v1/recovery/operations/{oid}/reconciliation", headers=headers)
    assert result.status_code == 401
    assert "observed" not in result.json() and "MOCK-SQ" not in result.text
    with service.db.transaction() as session:
        assert session.get(OperationRow, oid).status == "PENDING"
        assert session.get(RecoveryHoldRow, oid) is not None
    client.close()


def test_revoked_approval_is_diagnostic_and_matching_remote_stays_held(system):
    client, service, identities, headers, oid, payload = pilot_reader(system)
    with service.db.transaction(write=True) as session:
        operation = session.get(OperationRow, oid)
        session.get(ApprovalRow, operation.approval_id).approver_auth_version = "1:1"
    assert get(client, "/" + oid, headers)["approval"]["authority_current"] is True
    identities.set_membership("demo", "approver-01", "approver", active=False)
    assert get(client, "/" + oid, headers)["approval"]["authority_current"] is False
    system[2].create_draft(oid, payload)
    result = get(client, "/" + oid + "/reconciliation", headers)
    assert result["status"] == "verified" and result["replay_permitted"] is False
    assert get(client, "/" + oid, headers)["status"] == "PENDING"
    client.close()


def test_cost_proof_deep_differences_and_untrusted_extra_keys_are_bounded():
    from procureflow.erp import cost_mapping
    from procureflow.recovery import comparison
    payload = {"erp_mode": "erpnext", "erp_company": "Synthetic", "snapshot_hash": "a" * 64,
        "transaction_date": "2026-10-08", "total": "113.00", "erp_cost_accounts": {"tax": "Tax", "freight": "Freight"},
        "quote_values": {"supplier_id": "SUP-A", "sku": "STAND-01", "quantity": "1", "unit_price": "100.00",
            "currency": "CNY", "uom": "EA", "tax_mode": "excluded", "tax_rate": "0.13", "shipping_cost": "0.00",
            "discount": "0.00", "delivery_days": 7}}
    from procureflow.recovery import expected_record
    remote = expected_record(payload, "operation")
    remote["name"] = "SYNTHETIC-SQ"
    remote["cost_proof"] = deepcopy(cost_mapping(payload)[1])
    remote["cost_proof"]["taxes"][0]["tax_amount"] = "12.00"
    remote["cost_proof"]["SECRET-KEY"] = "SECRET-CONTENT"
    _, _, differences = comparison(remote, payload, "operation", None)
    assert "cost_proof.taxes[0].tax_amount" in {item["field"] for item in differences}
    assert "SECRET" not in json.dumps(differences)


@pytest.mark.parametrize("payload", [[], None, {"quote_collection": [{"document_id": {}, "version_id": [], "id": {"secret": "NEVER_SHOW"}}]}])
def test_malformed_local_snapshot_is_diagnostic_not_a_crash(system, payload):
    client, service, _ = system
    oid, _ = held(system)
    with service.db.transaction(write=True) as session:
        session.get(OperationRow, oid).payload = payload
    detail = get(client, "/" + oid)
    assert "LOCAL_SNAPSHOT_INTEGRITY_FAILED" in detail["diagnostics"]
    assert "NEVER_SHOW" not in json.dumps(detail)
    result = get(client, "/" + oid + "/reconciliation")
    assert result["status"] == "blocked"


def test_missing_expired_and_revoked_approval_never_grants_replay(system):
    client, service, _ = system
    oid, _ = held(system)
    with service.db.transaction(write=True) as session:
        approval = session.get(ApprovalRow, session.get(OperationRow, oid).approval_id)
        approval.status = "REJECTED"
        approval.expires_at = "2000-01-01T00:00:00+00:00"
    detail = get(client, "/" + oid)
    assert not detail["approval"]["unexpired"]
    assert {"APPROVAL_EXPIRED", "APPROVAL_NOT_CURRENT"} <= set(detail["diagnostics"])
    assert not detail["replay_permitted"]
