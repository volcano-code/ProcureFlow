"""Independent policy boundary regressions; ERP/model traffic stays local and fake."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from conftest import APPROVER, AUDITOR, BUYER, OTHER, approved, enqueue, ready, request, upload
from procureflow.advice import AdviceService
from procureflow.contracts import AdviceRunCommand, Principal
from procureflow.db import AdviceRunRow, ApprovalRow, PolicyVersionRow, QuoteVersionRow
from procureflow.domain import digest

BUYER_ID = Principal(user_id="buyer-01", tenant_id="demo", role="buyer")


def active(client, headers=BUYER):
    response = client.get("/api/v1/policy", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, *, headers=APPROVER, expected_version=None, **changes):
    if expected_version is None:
        expected_version = active(client, headers)["latest_version"]
    body = {"expected_version": expected_version, "budget_cap": None,
            "max_delivery_days": None, "minimum_valid_quotes": 1,
            "effective_at": None, "reason": "Independent policy regression", **changes}
    response = client.post("/api/v1/policy/versions", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def offer(client, request_id, supplier="SUP-A", price="1200.00", delivery=7):
    content = (f"supplier_id: {supplier}\nsku: STAND-01\nquantity: 20\nuom: EA\n"
               f"unit_price: {price}\ntax_mode: included\ntax_rate: 0.13\n"
               f"shipping_cost: 800.00\ndiscount: 0.00\ndelivery_days: {delivery}\ncurrency: CNY\n")
    quote = upload(client, request_id, filename=f"{supplier}.txt", content=content.encode())
    response = client.post(f"/api/v1/quotes/{quote['id']}/confirm", headers=BUYER,
                          json={"expected_version": quote["version"], "acknowledge": True})
    assert response.status_code == 200, response.text
    return response.json()


def analyze(client, request_id):
    response = client.post(f"/api/v1/requests/{request_id}/analyze", headers=BUYER, json={})
    assert response.status_code == 200, response.text
    return response.json()


def evaluations(client, request_id):
    response = client.get(f"/api/v1/requests/{request_id}/evaluations", headers=AUDITOR)
    assert response.status_code == 200, response.text
    return response.json()


def test_policy_is_tenant_scoped_and_publishing_is_role_scoped(system):
    client, _, _ = system
    initial_other = active(client, OTHER)
    first = active(client)
    for headers in (BUYER, AUDITOR):
        denied = client.post("/api/v1/policy/versions", headers=headers, json={
            "expected_version": first["latest_version"], "minimum_valid_quotes": 1,
            "reason": "Unauthorized publication"})
        assert denied.status_code == 403
    created = publish(client, budget_cap="1000.00")
    assert created["tenant_id"] == first["tenant_id"]
    assert active(client, OTHER) == initial_other
    assert created["id"] != initial_other["id"]
    req = request(client)
    analyze(client, req["id"])
    assert client.get(f"/api/v1/requests/{req['id']}/evaluations", headers=OTHER).status_code == 404


def test_two_simultaneous_publications_cannot_share_expected_version(system):
    client, service, _ = system
    version = active(client)["latest_version"]
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait(timeout=10)
        return client.post("/api/v1/policy/versions", headers=APPROVER, json={
            "expected_version": version, "minimum_valid_quotes": index + 1,
            "reason": "Concurrent revision test"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(result.status_code for result in results) == [201, 409]
    assert active(client)["latest_version"] == version + 1
    with service.db.transaction() as session:
        rows = list(session.scalars(select(PolicyVersionRow).where(PolicyVersionRow.tenant_id == "demo")))
        assert sorted(row.version for row in rows) == [1, 2]


def test_older_scheduled_version_never_supersedes_newer_effective_version(system, monkeypatch):
    from procureflow import policies
    client, _, _ = system
    future = datetime.now(timezone.utc) + timedelta(days=2)
    old = publish(client, minimum_valid_quotes=9, effective_at=future.isoformat())
    newer = publish(client, minimum_valid_quotes=2)
    assert old["version"] < newer["version"]
    monkeypatch.setattr(policies, "now", lambda: (future + timedelta(seconds=1)).isoformat())
    effective = active(client)
    assert effective["version"] == newer["version"]
    assert effective["minimum_valid_quotes"] == 2
    history = client.get("/api/v1/policy/versions", headers=BUYER).json()
    assert next(item for item in history if item["version"] == old["version"])["status"] == "superseded"


def test_repeated_supplier_quotes_do_not_manufacture_competition(system):
    client, _, _ = system
    publish(client, minimum_valid_quotes=2)
    req = request(client)
    offer(client, req["id"], price="1000.00")
    offer(client, req["id"], price="1100.00")
    blocked = analyze(client, req["id"])
    assert blocked["proposal"] is None
    assert evaluations(client, req["id"])[0]["result"]["valid_quote_count"] == 1
    offer(client, req["id"], supplier="SUP-B", price="1200.00")
    assert analyze(client, req["id"])["proposal"] is not None
    assert evaluations(client, req["id"])[0]["result"]["valid_quote_count"] == 2


@pytest.mark.parametrize("changes,violation", [
    ({"budget_cap": "24000.00"}, "BUDGET_EXCEEDED"),
    ({"max_delivery_days": 6}, "DELIVERY_EXCEEDS_LIMIT"),
])
def test_policy_caps_are_enforced_even_when_request_allows_offer(system, changes, violation):
    client, _, _ = system
    publish(client, **changes)
    req = request(client)
    quote = offer(client, req["id"])
    assert quote["calculation"]["eligible"] is False
    assert violation in quote["calculation"]["violations"]
    assert analyze(client, req["id"])["proposal"] is None


def test_policy_cannot_relax_stricter_request_limits(system):
    client, _, _ = system
    publish(client, budget_cap="999999.00", max_delivery_days=100)
    req = request(client)
    quote = offer(client, req["id"], price="2000.00", delivery=15)
    assert {"BUDGET_EXCEEDED", "DELIVERY_EXCEEDS_LIMIT"} <= set(quote["calculation"]["violations"])
    assert analyze(client, req["id"])["proposal"] is None


def test_approval_binds_unselected_quote_even_without_request_version_change(system):
    client, service, _ = system
    req = request(client)
    selected = offer(client, req["id"], supplier="SUP-A", price="1000.00")
    unselected = offer(client, req["id"], supplier="SUP-B", price="1200.00")
    proposal = analyze(client, req["id"])["proposal"]
    assert proposal["quote_id"] == selected["id"]
    approved_response = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
        json={"snapshot_hash": proposal["snapshot_hash"]})
    assert approved_response.status_code == 200, approved_response.text
    # Simulate a stale aggregate counter: the complete quote snapshot must still
    # protect the approval, not rely exclusively on request.version invalidation.
    with service.db.transaction(write=True) as session:
        version = session.get(QuoteVersionRow, unselected["version_id"])
        version.values = {**version.values, "unit_price": "1250.00"}
    response = client.post(f"/api/v1/requests/{req['id']}/execute", headers=BUYER,
                           json={"snapshot_hash": proposal["snapshot_hash"]})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "APPROVAL_STALE"


def test_invalidated_proposal_and_all_input_versions_remain_in_history(system):
    client, service, _ = system
    req = request(client)
    first = offer(client, req["id"], supplier="SUP-A", price="1000.00")
    second = offer(client, req["id"], supplier="SUP-B", price="1200.00")
    proposal = analyze(client, req["id"])["proposal"]
    approval = client.post(f"/api/v1/requests/{req['id']}/approval", headers=APPROVER,
        json={"snapshot_hash": proposal["snapshot_hash"]}).json()
    original = evaluations(client, req["id"])[0]
    frozen_input = deepcopy(original["input_snapshot"])
    assert first["version_id"] in json.dumps(frozen_input)
    assert second["version_id"] in json.dumps(frozen_input)
    changed = client.put(f"/api/v1/quotes/{second['id']}", headers=BUYER, json={
        "expected_version": second["version"], "values": {**second["values"], "unit_price": "1250.00"},
        "reason": "Correct unselected competitor amount"})
    assert changed.status_code == 200, changed.text
    old = next(item for item in evaluations(client, req["id"]) if item["id"] == original["id"])
    assert old["current"] is False
    assert old["input_snapshot"] == frozen_input
    assert old["result"]["proposal"] == proposal
    assert old["input_hash"] == digest(frozen_input)
    with service.db.transaction() as session:
        row = session.get(ApprovalRow, approval["id"])
        assert row.status == "STALE"
        assert row.snapshot == proposal


def test_advice_policy_is_immutable_during_run_and_stale_run_cannot_replay(system):
    client, service, erp = system
    req, _, _ = ready(client)
    advice = AdviceService(service)
    version = service.get_request(BUYER_ID, req["id"])["version"]
    run = advice.reserve(BUYER_ID, req["id"], AdviceRunCommand(expected_version=version,
                                                            idempotency_key="immutable-policy-0001"))
    before = active(client)
    calls = []

    class Agent:
        client = SimpleNamespace(close=lambda: None)

        def run(self, invoke, evidence_ids, observer=None):
            calls.append(1)
            captured = invoke("search_policy", {})
            publish(client, minimum_valid_quotes=2)
            assert invoke("search_policy", {}) == captured
            assert captured["policy_hash"] == before["policy_hash"]
            return {"summary": "Local test advice", "evidence_ids": sorted(evidence_ids)[:1],
                    "advisory_only": True}

    result = advice.process(BUYER_ID, run["id"], Agent)
    assert result["status"] == "STALE"
    assert result["current"] is False
    repeated = advice.process(BUYER_ID, run["id"], Agent)
    assert repeated["status"] == "STALE" and calls == [1]
    with service.db.transaction() as session:
        row = session.get(AdviceRunRow, run["id"])
        assert digest(row.input_snapshot) == run["input_hash"]
        assert row.input_snapshot["policy"]["policy_hash"] == before["policy_hash"]
    assert erp.count() == 0


def test_scheduled_policy_activation_during_remote_lookup_blocks_fresh_write(system, monkeypatch):
    from procureflow import policies
    client, service, erp = system
    req, _, proposal = approved(client)
    operation = enqueue(client, req, proposal)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    publish(client, minimum_valid_quotes=2, effective_at=future.isoformat())
    find = erp.find
    creates = []

    def activate_during_lookup(operation_id):
        result = find(operation_id)
        monkeypatch.setattr(policies, "now", lambda: (future + timedelta(seconds=1)).isoformat())
        return result

    monkeypatch.setattr(erp, "find", activate_during_lookup)
    monkeypatch.setattr(erp, "create_draft", lambda *args: creates.append(args))
    result = service.process_operation(BUYER_ID, operation["id"])
    assert result["status"] == "NEEDS_HUMAN"
    assert creates == [] and erp.count() == 0


def test_publication_waits_for_already_authorized_remote_write_boundary(system, monkeypatch):
    client, service, erp = system
    req, _, proposal = approved(client)
    operation = enqueue(client, req, proposal)
    entered, release, publishing = Event(), Event(), Event()
    create = erp.create_draft

    def paused_create(operation_id, payload):
        entered.set()
        assert release.wait(timeout=10)
        return create(operation_id, payload)

    def publish_concurrently():
        publishing.set()
        return publish(client, expected_version=1, minimum_valid_quotes=2)

    monkeypatch.setattr(erp, "create_draft", paused_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatched = pool.submit(service.process_operation, BUYER_ID, operation["id"])
        publication = None
        try:
            assert entered.wait(timeout=10)
            publication = pool.submit(publish_concurrently)
            assert publishing.wait(timeout=10)
            # The competing publish must wait on the held business lock. This
            # brief wait detects the former committed check/create race.
            assert not publication.done()
            with pytest.raises(TimeoutError):
                publication.result(timeout=0.15)
        finally:
            release.set()
        assert dispatched.result(timeout=15)["status"] == "COMPLETED"
        assert publication is not None and publication.result(timeout=15)["version"] == 2
    assert erp.count() == 1


def test_recovery_after_policy_change_only_reads_existing_remote_draft(system, monkeypatch):
    client, service, erp = system
    req, _, proposal = approved(client)
    operation = enqueue(client, req, proposal)
    erp.fail_after_commit_once.add(operation["id"])
    assert service.process_operation(BUYER_ID, operation["id"])["status"] == "RECONCILING"
    assert erp.count() == 1
    publish(client, minimum_valid_quotes=2)
    monkeypatch.setattr(erp, "create_draft", lambda *_: pytest.fail("Recovery must never create a second draft"))
    recovered = service.process_operation(BUYER_ID, operation["id"])
    assert recovered["status"] == "COMPLETED"
    assert erp.count() == 1


def test_concurrent_first_service_initialization_has_one_default_policy(tmp_path, request):
    from procureflow.config import Settings
    from procureflow.db import Database
    from procureflow.erp import MockERP
    from procureflow.service import ProcurementService

    if os.getenv("PF_TEST_BACKEND") == "postgresql":
        database = request.getfixturevalue("pg_database")
        settings = Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False))
    else:
        settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'bootstrap.sqlite3'}")
        database = Database(settings.database_url, create_schema=True)
    erp = MockERP(tmp_path / "bootstrap-erp.sqlite3")
    barrier = Barrier(4)

    def start(_):
        barrier.wait(timeout=10)
        service = ProcurementService(database, settings, erp)
        return service.policy(BUYER_ID)

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            initialized = list(pool.map(start, range(4)))
        assert len({row["id"] for row in initialized}) == 1
        assert all(row["version"] == row["latest_version"] == 1 for row in initialized)
        with database.transaction() as session:
            revisions = list(session.scalars(select(PolicyVersionRow).where(PolicyVersionRow.tenant_id == "demo")))
            assert len(revisions) == 1
    finally:
        database.engine.dispose()


def test_activation_during_final_snapshot_validation_blocks_write(system, monkeypatch):
    from procureflow import policies
    client, service, erp = system
    req, _, proposal = approved(client)
    operation = enqueue(client, req, proposal)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    publish(client, minimum_valid_quotes=2, effective_at=future.isoformat())
    verify = service._verify_document
    checks, creates = [], []

    def activation_during_slow_source_verification(document):
        result = verify(document)
        checks.append(document.id)
        if len(checks) == 2:
            # First source verification is the dispatch claim. Second is inside
            # the final approval gate, after the binding's policy was selected.
            monkeypatch.setattr(policies, "now", lambda: (future + timedelta(seconds=1)).isoformat())
        return result

    monkeypatch.setattr(service, "_verify_document", activation_during_slow_source_verification)
    monkeypatch.setattr(erp, "create_draft", lambda *args: creates.append(args))
    result = service.process_operation(BUYER_ID, operation["id"])
    assert len(checks) >= 2
    assert result["status"] == "NEEDS_HUMAN"
    assert creates == [] and erp.count() == 0


def test_modified_persisted_advice_snapshot_is_rejected_without_model_replay(system):
    client, service, _ = system
    req, _, _ = ready(client)
    advice = AdviceService(service)
    version = service.get_request(BUYER_ID, req["id"])["version"]
    run = advice.reserve(BUYER_ID, req["id"], AdviceRunCommand(expected_version=version,
                                                            idempotency_key="integrity-check-0001"))
    with service.db.transaction(write=True) as session:
        row = session.get(AdviceRunRow, run["id"])
        modified = deepcopy(row.input_snapshot)
        modified["policy"]["minimum_valid_quotes"] = 0
        row.input_snapshot = modified
    assert advice.get(BUYER_ID, run["id"])["current"] is False
    listed = next(row for row in advice.list(BUYER_ID, req["id"]) if row["id"] == run["id"])
    assert listed["current"] is False

    def forbidden_factory():
        pytest.fail("A modified stored snapshot must not be sent to a model")

    result = advice.process(BUYER_ID, run["id"], forbidden_factory)
    assert result["status"] == "STALE"
    assert result["current"] is False
    assert advice.process(BUYER_ID, run["id"], forbidden_factory)["status"] == "STALE"


def test_approval_expiring_during_final_snapshot_validation_blocks_write(system, monkeypatch):
    from procureflow import service as service_module
    client, service, erp = system
    req, _, proposal = approved(client)
    operation = enqueue(client, req, proposal)
    verify = service._verify_document
    checks, creates = [], []

    def expiration_during_slow_source_verification(document):
        result = verify(document)
        checks.append(document.id)
        if len(checks) == 2:
            expired_time = datetime.now(timezone.utc) + timedelta(days=1)
            monkeypatch.setattr(service_module, "now", lambda: expired_time.isoformat())
        return result

    monkeypatch.setattr(service, "_verify_document", expiration_during_slow_source_verification)
    monkeypatch.setattr(erp, "create_draft", lambda *args: creates.append(args))
    result = service.process_operation(BUYER_ID, operation["id"])
    assert len(checks) >= 2
    assert result["status"] == "NEEDS_HUMAN"
    assert result["error"] == "APPROVAL_EXPIRED"
    assert creates == [] and erp.count() == 0
