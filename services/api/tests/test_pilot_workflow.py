"""Pilot authority at business boundaries; all credentials/data are disposable fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

import pytest
from sqlalchemy import func, select

from procureflow.auth import IdentityService
from procureflow.config import Settings
from procureflow.contracts import (AdviceRunCommand, ApprovalCommand, Principal, QuoteConfirm,
                                  RequestCreate)
from procureflow.db import (ApprovalRow, Database, DocumentRow, OperationRow, OutboxRow,
                           PilotSessionRow, RequestRow)
from procureflow.erp import MockERP, ERPUnknown
from procureflow.errors import DomainError
from procureflow.service import ProcurementService
from procureflow.advice import AdviceService

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def pilot(tmp_path, request):
    if os.getenv("PF_TEST_BACKEND") == "postgresql":
        db = request.getfixturevalue("pg_database")
    else:
        db = Database(f"sqlite:///{tmp_path}/pilot.sqlite3", create_schema=True)
    settings = Settings(data_dir=tmp_path, database_url=db.engine.url.render_as_string(hide_password=False), mode="pilot")
    erp = MockERP(tmp_path / "mock-erp.sqlite3")
    service = ProcurementService(db, settings, erp)
    identities = service.identities
    principals = {}
    for tenant, user, role in (("alpha", "buyer", "buyer"), ("alpha", "approver", "approver"),
                              ("alpha", "auditor", "auditor"), ("beta", "buyer", "buyer")):
        if tenant not in principals:
            identities.create_tenant(tenant)
            principals[tenant] = {}
        identities.set_membership(tenant, user, role)
        login = identities.login(identities.issue_invite(tenant, user)["credential"])
        principals[tenant][user] = identities.authenticate(login["token"])
    yield service, erp, principals
    if db.sqlite:
        db.engine.dispose()


def prepared(pilot):
    service, erp, principals = pilot
    buyer, approver = principals["alpha"]["buyer"], principals["alpha"]["approver"]
    request = service.create_request(buyer, RequestCreate(title="Synthetic pilot request", sku="STAND-01",
        quantity="20", budget="30000.00", max_delivery_days=14))
    quote = service.import_quote(buyer, request["id"], "supplier-a.txt", (ROOT / "evals/fixtures/supplier-a.txt").read_bytes())
    service.confirm_quote(buyer, quote["id"], QuoteConfirm(expected_version=1, acknowledge=True))
    proposal = service.analyze(buyer, request["id"])["proposal"]
    service.approve(approver, request["id"], ApprovalCommand(snapshot_hash=proposal["snapshot_hash"]))
    return request, proposal


def reserved(pilot):
    request, proposal = prepared(pilot)
    service, _, identities = pilot
    return request, proposal, service.enqueue(identities["alpha"]["buyer"], request["id"], proposal["snapshot_hash"])


def assert_denied(call, code="UNAUTHENTICATED"):
    with pytest.raises(DomainError) as caught:
        call()
    assert caught.value.code == code


def expire_session(service, principal):
    with service.db.transaction(write=True) as session:
        session.get(PilotSessionRow, principal.session_id).expires_at = "2000-01-01T00:00:00+00:00"


@pytest.mark.parametrize("change", ["logout", "expiry", "revoke", "role", "tenant"])
def test_cached_principal_cannot_read_or_mutate_after_authority_change(pilot, change):
    service, erp, identities = pilot
    buyer = identities["alpha"]["buyer"]
    request, _ = prepared(pilot)
    if change == "logout":
        service.identities.logout(buyer)
    elif change == "expiry":
        expire_session(service, buyer)
    elif change == "revoke":
        service.identities.set_membership("alpha", "buyer", "buyer", active=False)
    elif change == "role":
        service.identities.set_membership("alpha", "buyer", "auditor")
    else:
        service.identities.set_tenant_active("alpha", False)
    assert_denied(lambda: service.list_requests(buyer))
    assert_denied(lambda: service.get_request(buyer, request["id"]))
    assert_denied(lambda: service.events(buyer, request["id"]))
    assert_denied(lambda: service.analyze(buyer, request["id"]))
    assert_denied(lambda: AdviceService(service).reserve(buyer, request["id"],
        AdviceRunCommand(expected_version=2, idempotency_key="invalid-auth")))
    assert erp.count() == 0


def test_pilot_rejects_forged_worker_and_changed_role_principals(pilot):
    service, _, identities = pilot
    request, proposal, operation = reserved(pilot)
    forged = Principal(user_id="outbox-worker", tenant_id="alpha", role="buyer")
    assert_denied(lambda: service.process_operation(forged, operation["id"]))
    changed = identities["alpha"]["buyer"].model_copy(update={"role": "approver"})
    assert_denied(lambda: service.approve(changed, request["id"], ApprovalCommand(snapshot_hash=proposal["snapshot_hash"])))
    assert_denied(lambda: service.get_request(identities["beta"]["buyer"], request["id"]), "NOT_FOUND")


@pytest.mark.parametrize("change", ["revoke", "role", "reenable", "tenant_reenable"])
def test_worker_never_posts_for_obsolete_approver_generation(pilot, change, monkeypatch):
    service, erp, identities = pilot
    request, proposal, operation = reserved(pilot)
    if change == "role":
        service.identities.set_membership("alpha", "approver", "auditor")
    elif change == "tenant_reenable":
        service.identities.set_tenant_active("alpha", False)
        service.identities.set_tenant_active("alpha", True)
    else:
        service.identities.set_membership("alpha", "approver", "approver", active=False)
        if change == "reenable":
            service.identities.set_membership("alpha", "approver", "approver", active=True)
    calls = []
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    # A newly constructed process service sees the same durable authority without restart config.
    other_db = Database(service.settings.database_url)
    try:
        restarted = ProcurementService(other_db, service.settings, erp)
        result = restarted.process_pending_operation("alpha", operation["id"])
    finally:
        other_db.engine.dispose()
    assert result["status"] == "NEEDS_HUMAN"
    assert result["error"] == ("BUYER_REVOKED" if change == "tenant_reenable" else "APPROVER_REVOKED")
    assert calls == [] and erp.count() == 0
    with service.db.transaction() as session:
        assert session.get(OperationRow, operation["id"]).attempts == 0
        assert session.scalar(select(OutboxRow).where(OutboxRow.operation_id == operation["id"])).status == "DONE"


def test_revocation_during_remote_lookup_blocks_first_post(pilot, monkeypatch):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    def lookup(_):
        service.identities.set_membership("alpha", "approver", "approver", active=False)
        return None
    monkeypatch.setattr(erp, "find", lookup)
    calls = []
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == "NEEDS_HUMAN" and result["error"] == "APPROVER_REVOKED"
    assert calls == []


def test_caller_logout_during_lookup_blocks_direct_api_dispatch(pilot, monkeypatch):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    buyer = identities["alpha"]["buyer"]
    def lookup(_):
        service.identities.logout(buyer)
        return None
    monkeypatch.setattr(erp, "find", lookup)
    calls = []
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    assert_denied(lambda: service.process_operation(buyer, operation["id"]))
    with service.db.transaction() as session:
        result = session.get(OperationRow, operation["id"])
        assert result.status == "NEEDS_HUMAN" and result.error == "UNAUTHENTICATED"
    assert calls == []


def test_committed_revocation_serializes_after_already_authorized_post(pilot, monkeypatch):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    entered, release, attempted, finished = Event(), Event(), Event(), Event()
    original = erp.create_draft
    calls = []
    def create(key, payload):
        calls.append(key)
        entered.set()
        assert release.wait(10)
        return original(key, payload)
    def revoke():
        attempted.set()
        service.identities.set_membership("alpha", "approver", "approver", active=False)
        finished.set()
    monkeypatch.setattr(erp, "create_draft", create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(service.process_pending_operation, "alpha", operation["id"])
        try:
            assert entered.wait(10)
            removal = pool.submit(revoke)
            assert attempted.wait(10)
            assert not finished.wait(.15), "Revocation must not commit between auth gate and first POST"
        finally:
            release.set()
        result = dispatch.result(timeout=15)
        removal.result(timeout=15)
    assert result["status"] == "COMPLETED" and calls == [operation["id"]] and finished.is_set()
    # Idempotent read-only worker processing remains possible after authority was revoked.
    assert service.process_pending_operation("alpha", operation["id"])["status"] == "COMPLETED"
    assert calls == [operation["id"]]


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_result_recovery_after_revoke_is_read_only(pilot, monkeypatch, committed):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    original, calls = erp.create_draft, []
    def uncertain(key, payload):
        calls.append(key)
        if committed:
            original(key, payload)
        raise ERPUnknown("synthetic lost receipt")
    monkeypatch.setattr(erp, "create_draft", uncertain)
    assert service.process_pending_operation("alpha", operation["id"])["status"] == "RECONCILING"
    service.identities.set_membership("alpha", "approver", "approver", active=False)
    service.identities.logout(identities["alpha"]["buyer"])
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == ("COMPLETED" if committed else "NEEDS_HUMAN")
    assert calls == [operation["id"]] and erp.count() == int(committed)
    if not committed:
        assert result["error"] == "REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT"


def test_logout_does_not_silently_withdraw_accepted_business_approval(pilot):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    service.identities.logout(identities["alpha"]["buyer"])
    service.identities.logout(identities["alpha"]["approver"])
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == "COMPLETED" and erp.count() == 1


def test_logout_during_parser_cannot_commit_source_or_quote(pilot, monkeypatch):
    import procureflow.service as module
    service, _, identities = pilot
    buyer = identities["alpha"]["buyer"]
    request = service.create_request(buyer, RequestCreate(title="Parser revocation", sku="STAND-01", quantity="20", budget="30000.00"))
    original = module.parse_document
    def delayed(*args):
        parsed = original(*args)
        service.identities.logout(buyer)
        return parsed
    monkeypatch.setattr(module, "parse_document", delayed)
    assert_denied(lambda: service.import_quote(buyer, request["id"], "supplier-a.txt", (ROOT / "evals/fixtures/supplier-a.txt").read_bytes()))
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(DocumentRow)) == 0
    assert not list(service.document_dir.iterdir())


def test_read_revalidates_after_slow_snapshot(pilot, monkeypatch):
    service, _, identities = pilot
    request, _ = prepared(pilot)
    buyer = identities["alpha"]["buyer"]
    original = service._request_dto
    def delayed(*args):
        result = original(*args)
        service.identities.logout(buyer)
        return result
    monkeypatch.setattr(service, "_request_dto", delayed)
    assert_denied(lambda: service.get_request(buyer, request["id"]))


def test_expiry_before_business_commit_rolls_back(pilot, monkeypatch):
    import procureflow.auth as auth
    service, _, identities = pilot
    buyer = identities["alpha"]["buyer"]
    original = service._request_dto
    def delayed(*args):
        result = original(*args)
        monkeypatch.setattr(auth, "_unexpired", lambda _: False)
        return result
    monkeypatch.setattr(service, "_request_dto", delayed)
    assert_denied(lambda: service.create_request(buyer, RequestCreate(title="Expired at commit", sku="STAND-01", quantity="20", budget="30000.00")))
    with service.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(RequestRow)) == 0


def test_independent_worker_restart_reads_revocation_without_token_config(pilot):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    service.identities.set_membership("alpha", "approver", "approver", active=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PF_", "ERP_", "LLM_"))}
    env.update(PYTHONPATH=str(ROOT / "services/api"), PF_MODE="pilot", PF_ERP_MODE="mock",
        PF_DATA_DIR=str(service.settings.data_dir), PF_DATABASE_URL=service.settings.database_url, ERP_ALLOW_DRAFT_WRITES="false")
    result = subprocess.run([sys.executable, "-m", "procureflow.worker", "--once"], cwd=ROOT,
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "Pilot worker failed; inspect private local logs"
    with service.db.transaction() as session:
        row = session.get(OperationRow, operation["id"])
        assert row.status == "NEEDS_HUMAN" and row.error == "APPROVER_REVOKED"
    assert erp.count() == 0


@pytest.mark.parametrize("change", ["disabled", "role", "reenabled", "expiry", "legacy_unbound"])
def test_revoked_execution_initiator_cannot_authorize_worker_first_post(pilot, monkeypatch, change):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    if change == "role":
        service.identities.set_membership("alpha", "buyer", "auditor")
    elif change == "expiry":
        service.identities.set_membership("alpha", "buyer", "buyer", expires_at="2000-01-01T00:00:00+00:00")
    elif change == "legacy_unbound":
        with service.db.transaction(write=True) as session:
            session.get(OperationRow, operation["id"]).initiator_auth_version = None
    else:
        service.identities.set_membership("alpha", "buyer", "buyer", active=False)
        if change == "reenabled":
            service.identities.set_membership("alpha", "buyer", "buyer", active=True)
    calls = []
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == "NEEDS_HUMAN" and result["error"] == "BUYER_REVOKED"
    assert calls == [] and erp.count() == 0


def test_execution_initiator_revoked_during_lookup_is_rechecked(pilot, monkeypatch):
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    def lookup(_):
        service.identities.set_membership("alpha", "buyer", "buyer", active=False)
        return None
    monkeypatch.setattr(erp, "find", lookup)
    calls = []
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == "NEEDS_HUMAN" and result["error"] == "BUYER_REVOKED"
    assert calls == []


def test_advice_receipt_survives_logout_but_is_not_disclosed(pilot):
    from procureflow.db import AdviceRunRow
    from types import SimpleNamespace
    service, _, identities = pilot
    request, _ = prepared(pilot)
    buyer = identities["alpha"]["buyer"]
    advice = AdviceService(service)
    current = service.get_request(buyer, request["id"])
    run = advice.reserve(buyer, request["id"], AdviceRunCommand(expected_version=current["version"], idempotency_key="logout-in-provider"))
    class Agent:
        client = SimpleNamespace(close=lambda: None)
        def run(self, *args, **kwargs):
            service.identities.logout(buyer)
            return {"summary": "Synthetic already finished response"}
    assert_denied(lambda: advice.process(buyer, run["id"], Agent))
    with service.db.transaction() as session:
        stored = session.get(AdviceRunRow, run["id"])
        assert stored.status == "COMPLETED"
        assert stored.output == {"summary": "Synthetic already finished response"}
    assert_denied(lambda: advice.process(buyer, run["id"], Agent))


def test_buyer_membership_expiry_during_slow_snapshot_denies_first_post(pilot, monkeypatch):
    from procureflow.db import PilotMembershipRow
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    # Inject a short future deadline without changing authorization generation:
    # this tests the passage of time rather than administrative withdrawal.
    with service.db.transaction(write=True) as session:
        member = session.get(PilotMembershipRow, ("alpha", "buyer"))
        member.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=.5)).isoformat()
    original = service._current_snapshot
    calls, checks = [], []
    def slow(*args):
        result = original(*args)
        checks.append(1)
        if len(checks) == 2:
            time.sleep(.6)
        return result
    monkeypatch.setattr(service, "_current_snapshot", slow)
    monkeypatch.setattr(erp, "create_draft", lambda *args: calls.append(args))
    result = service.process_pending_operation("alpha", operation["id"])
    assert result["status"] == "NEEDS_HUMAN" and result["error"] == "BUYER_REVOKED"
    assert calls == [] and erp.count() == 0


@pytest.mark.parametrize("target", ["buyer", "approver", "approval", "caller-session", "other-caller-member"])
def test_real_adapter_rechecks_after_metadata_gets_before_post(pilot, monkeypatch, target):
    """Actual ERPNext adapter, synthetic HTTP transport; not a live ERP claim."""
    from dataclasses import replace
    import httpx
    import json
    from procureflow.db import PilotMembershipRow
    from procureflow.erp import ERPNextClient
    from test_erp_cost_mapping import TAX_ACCOUNT, FREIGHT_ACCOUNT
    service, _, identities = pilot
    calls, company_reads = [], []
    def transport(request):
        calls.append((request.method, request.url.path))
        if '/Company/' in request.url.path:
            company_reads.append(1)
            if len(company_reads) == 2:
                time.sleep(.6)
            return httpx.Response(200, json={"data": {"name": "Demo", "default_currency": "CNY"}})
        if request.url.path.endswith('/Custom Field'):
            field = json.loads(request.url.params['filters'])[-1][-1]
            return httpx.Response(200, json={"data": [{"fieldname": field, "unique": 1, "fieldtype": "Data"}]})
        if request.method == "POST":
            return httpx.Response(500, json={"error": "Unexpected write"})
        return httpx.Response(200, json={"data": []})
    adapter = ERPNextClient('https://erp.test', 'synthetic-key', 'synthetic-secret', 'Demo', True,
        transport=httpx.MockTransport(transport), tax_account=TAX_ACCOUNT, freight_account=FREIGHT_ACCOUNT)
    service.erp = adapter
    service.settings = replace(service.settings, erp_mode='erpnext', erp_url='https://erp.test',
        erp_company='Demo', erp_allow_draft_writes=True, erp_tax_account=TAX_ACCOUNT, erp_freight_account=FREIGHT_ACCOUNT)
    try:
        _, _, operation = reserved((service, adapter, identities))
        caller = identities['alpha']['buyer']
        if target == 'other-caller-member':
            service.identities.set_membership('alpha', 'second-buyer', 'buyer')
            login = service.identities.login(service.identities.issue_invite('alpha', 'second-buyer')['credential'])
            caller = service.identities.authenticate(login['token'])
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=.5)).isoformat()
        with service.db.transaction(write=True) as session:
            if target == 'approval':
                row = session.get(OperationRow, operation['id'])
                session.get(ApprovalRow, row.approval_id).expires_at = deadline
            elif target == 'caller-session':
                session.get(PilotSessionRow, caller.session_id).expires_at = deadline
            else:
                who = 'second-buyer' if target == 'other-caller-member' else target
                session.get(PilotMembershipRow, ('alpha', who)).expires_at = deadline
        if target in {'caller-session', 'other-caller-member'}:
            assert_denied(lambda: service.process_operation(caller, operation['id']))
            with service.db.transaction() as session:
                result = session.get(OperationRow, operation['id'])
                assert result.status == 'NEEDS_HUMAN' and result.error == 'UNAUTHENTICATED'
        else:
            result = service.process_pending_operation('alpha', operation['id'])
            assert result['status'] == 'NEEDS_HUMAN'
            assert result['error'] == {'buyer': 'BUYER_REVOKED', 'approver': 'APPROVER_REVOKED', 'approval': 'APPROVAL_EXPIRED'}[target]
        assert len(company_reads) == 2
        assert not any(method == 'POST' for method, _ in calls)
    finally:
        adapter.client.close()


def test_policy_activation_during_final_identity_validation_blocks_post(pilot, monkeypatch):
    from procureflow.contracts import PolicyVersionCreate
    service, erp, identities = pilot
    _, _, operation = reserved(pilot)
    service.publish_policy(identities['alpha']['approver'], PolicyVersionCreate(
        expected_version=1, minimum_valid_quotes=2,
        effective_at=datetime.now(timezone.utc)+timedelta(seconds=.5), reason='Synthetic scheduled policy race'))
    original, checks, calls = service._validate_execution_authority, [], []
    def slow(*args):
        result=original(*args)
        checks.append(1)
        if len(checks)==3:
            time.sleep(.6)
        return result
    monkeypatch.setattr(service,'_validate_execution_authority',slow)
    monkeypatch.setattr(erp,'create_draft',lambda *args:calls.append(args))
    result=service.process_pending_operation('alpha',operation['id'])
    assert result['status']=='NEEDS_HUMAN' and result['error']=='APPROVAL_STALE'
    assert calls==[] and erp.count()==0
