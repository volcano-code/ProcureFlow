"""Tenant-policy contracts and durable history. Offline; ERP/model calls mocked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import APPROVER, AUDITOR, BUYER, OTHER, approved, publish_policy, ready, request
from procureflow.db import AdviceRunRow, EvaluationRow, PolicyVersionRow
from procureflow.domain import digest


def policy(client, headers=BUYER):
    response = client.get("/api/v1/policy", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_bootstrap_is_persisted_once_and_default_keeps_request_caps(system):
    client, service, _ = system
    initial = policy(client)
    assert initial["version"] == initial["latest_version"] == 1
    assert initial["budget_cap"] is None and initial["max_delivery_days"] is None
    assert initial["minimum_valid_quotes"] == 1 and initial["status"] == "effective"
    assert initial["created_by"] == "system"
    assert policy(client, AUDITOR) == initial
    from procureflow.service import ProcurementService
    ProcurementService(service.db, service.settings, service.erp)
    assert policy(client) == initial
    assert len(client.get("/api/v1/policy/versions", headers=BUYER).json()) == 1
    assert policy(client, OTHER)["id"] != initial["id"]
    _, _, proposal = ready(client)
    assert proposal["policy"]["id"] == initial["id"]
    assert proposal["policy_version"] == 1


@pytest.mark.parametrize("headers", [BUYER, AUDITOR, OTHER])
def test_only_approver_can_publish(system, headers):
    client, _, _ = system
    response = client.post("/api/v1/policy/versions", headers=headers, json={
        "expected_version": 1, "minimum_valid_quotes": 1, "reason": "Unauthorized revision"})
    assert response.status_code == 403
    assert policy(client)["version"] == 1


@pytest.mark.parametrize("change", [
    {"expected_version": True}, {"expected_version": "1"}, {"expected_version": 0},
    {"budget_cap": 1.1}, {"budget_cap": True}, {"budget_cap": "NaN"}, {"budget_cap": "-1"},
    {"budget_cap": "1.001"}, {"budget_cap": "1000000001.00"},
    {"max_delivery_days": True}, {"max_delivery_days": 0}, {"max_delivery_days": 366},
    {"minimum_valid_quotes": True}, {"minimum_valid_quotes": "2"},
    {"minimum_valid_quotes": 0}, {"minimum_valid_quotes": 101},
    {"reason": "bad"}, {"reason": " " * 10}, {"reason": "x" * 501},
    {"effective_at": "2026-10-02T12:00:00"}, {"effective_at": 9999999999},
    {"effective_at": "not-a-date"}, {"tenant_id": "other"}, {"version": 500}, {"policy_hash": "forged"},
])
def test_policy_rejects_invalid_or_server_owned_fields(system, change):
    client, _, _ = system
    before = policy(client)
    response = client.post("/api/v1/policy/versions", headers=APPROVER, json={
        "expected_version": 1, "minimum_valid_quotes": 1, "reason": "Valid policy reason", **change})
    assert response.status_code == 422, response.text
    assert policy(client) == before


def test_reason_author_and_policy_hash_are_immutable_history(system):
    client, service, _ = system
    first = policy(client)
    second = publish_policy(client, budget_cap="10000.00", max_delivery_days=5,
                            minimum_valid_quotes=2, reason="Require competitive offers")
    assert second["version"] == 2 and second["created_by"] == "approver-01"
    assert second["reason"] == "Require competitive offers"
    immutable = {k: v for k, v in second.items() if k not in {"policy_hash", "status", "latest_version"}}
    assert digest(immutable) == second["policy_hash"]
    history = client.get("/api/v1/policy/versions", headers=AUDITOR).json()
    assert [item["version"] for item in history] == [2, 1]
    assert history[1]["policy_hash"] == first["policy_hash"] and history[1]["status"] == "superseded"
    with pytest.raises(ValueError, match="IMMUTABLE_HISTORY"):
        with service.db.transaction(write=True) as session:
            row = session.get(PolicyVersionRow, first["id"])
            row.reason = "Rewrite old history"
    assert client.get("/api/v1/policy/versions", headers=AUDITOR).json() == history


def test_request_changes_preserve_exact_evaluation_and_approval_snapshots(system):
    client, service, _ = system
    req, quote, proposal = approved(client)
    old = client.get(f"/api/v1/requests/{req['id']}/evaluations", headers=AUDITOR).json()[0]
    assert old["current"] and old["created_by"] == "buyer-01"
    assert old["input_snapshot"]["quote_collection"][0]["version_id"] == quote["version_id"]
    assert old["input_snapshot"]["quote_collection"][0]["evidence"] == quote["evidence"]
    values = {**quote["values"], "unit_price": "1150.00"}
    response = client.put(f"/api/v1/quotes/{quote['id']}", headers=BUYER, json={
        "expected_version": quote["version"], "values": values, "reason": "Supplier revised amount"})
    assert response.status_code == 200
    historical = client.get(f"/api/v1/requests/{req['id']}/evaluations", headers=AUDITOR).json()[0]
    assert not historical["current"] and historical["stale_reason"]
    assert historical["input_snapshot"] == old["input_snapshot"] and historical["result"] == old["result"]
    approval = client.get(f"/api/v1/requests/{req['id']}/approvals", headers=AUDITOR).json()[0]
    assert approval["snapshot"] == proposal and approval["status"] == "STALE" and not approval["current"]
    assert approval["snapshot_hash"] == proposal["snapshot_hash"]
    with pytest.raises(ValueError, match="IMMUTABLE_HISTORY"):
        with service.db.transaction(write=True) as session:
            session.get(EvaluationRow, old["id"]).result = {"proposal": None}


def test_insufficient_supplier_count_still_preserves_blocked_evaluation(system):
    client, _, _ = system
    req, _, _ = ready(client)
    publish_policy(client, minimum_valid_quotes=2)
    response = client.post(f"/api/v1/requests/{req['id']}/analyze", headers=BUYER, json={})
    result = response.json()
    assert result["proposal"] is None and result["valid_quote_count"] == 1
    assert result["violations"] == ["INSUFFICIENT_VALID_QUOTES"]
    assert result["evaluation"]["current"]
    assert result["evaluation"]["result"]["proposal"] is None
    assert len(client.get(f"/api/v1/requests/{req['id']}/evaluations", headers=AUDITOR).json()) == 2


def test_cross_tenant_history_routes_are_hidden(system):
    client, _, _ = system
    req, _, _ = ready(client)
    for suffix in ("evaluations", "approvals"):
        response = client.get(f"/api/v1/requests/{req['id']}/{suffix}", headers=OTHER)
        assert response.status_code == 404


def test_backdate_is_denied_and_offset_time_is_normalized(system):
    client, _, _ = system
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    response = client.post("/api/v1/policy/versions", headers=APPROVER, json={
        "expected_version": 1, "minimum_valid_quotes": 1, "reason": "No retroactive changes", "effective_at": old})
    assert response.status_code == 422 and response.json()["error"]["code"] == "POLICY_BACKDATE_DENIED"
    instant = datetime.now(timezone.utc) + timedelta(days=2)
    offset = timezone(timedelta(hours=8))
    created = publish_policy(client, effective_at=instant.astimezone(offset).isoformat())
    assert created["effective_at"] == instant.isoformat() and created["status"] == "scheduled"
    assert policy(client)["version"] == 1 and policy(client)["latest_version"] == 2


def test_legacy_advice_without_input_snapshot_fails_closed(system):
    client, service, _ = system
    req = request(client)
    from procureflow.contracts import Principal
    from procureflow.advice import AdviceService
    with service.db.transaction(write=True) as session:
        row = AdviceRunRow(id="adv_legacy", tenant_id="demo", request_id=req["id"], actor_id="buyer-01",
            idempotency_key="legacy-migration-key", request_version=req["version"], input_hash="0" * 64,
            input_snapshot=None, status="PENDING")
        session.add(row)
    invoked = []
    result = AdviceService(service).process(Principal(user_id="buyer-01", tenant_id="demo", role="buyer"),
        "adv_legacy", lambda: invoked.append(True))
    assert result["status"] == "STALE" and result["stale_reason"] == "LEGACY_INPUT_UNBOUND"
    assert not result["current"] and not invoked


def test_history_pagination_retrieves_older_evaluations_and_approvals(system):
    client, _, _ = system
    req, _, proposal = ready(client)
    for _ in range(3):
        analyzed = client.post(f"/api/v1/requests/{req['id']}/analyze", headers=BUYER, json={})
        assert analyzed.status_code == 200
        approved_response = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
                                       json={"snapshot_hash": proposal["snapshot_hash"]})
        assert approved_response.status_code == 200
    for suffix, count in (("evaluations", 4), ("approvals", 3)):
        url = f"/api/v1/requests/{req['id']}/{suffix}"
        all_rows = client.get(url, headers=AUDITOR).json()
        paged = [client.get(url, params={"offset": index, "limit": 1}, headers=AUDITOR).json()[0]
                 for index in range(count)]
        assert [row["id"] for row in paged] == [row["id"] for row in all_rows]
        assert client.get(url, params={"offset": count, "limit": 1}, headers=AUDITOR).json() == []
        for bad in ({"offset": -1}, {"limit": 0}, {"limit": 201}):
            assert client.get(url, params=bad, headers=AUDITOR).status_code == 422
