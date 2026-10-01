"""Durable read-only advice lifecycle. All model traffic is mocked/offline."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import APPROVER, AUDITOR, BUYER, OTHER, approved, ready, request, upload
from procureflow.agent import ReadOnlyAgent
from procureflow.app import create_app
from procureflow.db import Base, Database
from procureflow.errors import DomainError


class CountingAgent:
    """The adapter contract, without making network calls or changing procurement."""

    def __init__(self):
        self.calls = 0
        self.factory_calls = 0
        self.closed = 0
        self.effect = None
        self.before_tools = None
        self.error = None
        self.factory_error = None
        self.comparisons = []
        self.policies = []
        self.sources = []
        self.lock = Lock()
        self.client = SimpleNamespace(close=self.close)

    def close(self):
        self.closed += 1

    def factory(self):
        self.factory_calls += 1
        if self.factory_error:
            raise self.factory_error
        return self

    def run(self, invoke, evidence_ids, observer=None):
        with self.lock:
            self.calls += 1
        if self.before_tools:
            self.before_tools()
        comparison = invoke("get_comparison", {})
        self.comparisons.append(comparison)
        self.policies.append(invoke("search_policy", {}))
        for document_id in sorted({q["document_id"] for q in comparison["quotes"]}):
            self.sources.append(invoke("get_evidence", {"document_id": document_id}))
        if observer:
            observer({"type": "model_completed", "model_call": 1, "model": "offline-test", "usage": None})
        if self.effect:
            self.effect()
        if self.error:
            raise self.error
        return {
            "summary": "只读证据说明，不构成采购审批。",
            "evidence_ids": sorted(evidence_ids)[:1],
            "runtime": "bounded-read-only-tool-loop",
            "llm_used": True,
            "advisory_only": True,
            "semantic_factuality_verified": False,
            "model_calls": 1,
            "tool_calls": 2 + len(self.sources),
            "trace": [{"type": "model_completed", "model_call": 1, "model": "offline-test", "usage": None}],
            "usage": None,
            "usage_complete": False,
            "usage_source": "provider_reported",
            "cost": None,
        }


@pytest.fixture
def agent(monkeypatch):
    result = CountingAgent()
    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(result.factory))
    return result


def get_request(client, request_id):
    response = client.get(f"/api/v1/requests/{request_id}", headers=BUYER)
    assert response.status_code == 200, response.text
    return response.json()


def create_run(client, request_id, key="advice-key-0001", version=None, headers=BUYER):
    if version is None:
        version = get_request(client, request_id)["version"]
    response = client.post(f"/api/v1/requests/{request_id}/advice-runs", headers=headers,
                           json={"expected_version": version, "idempotency_key": key})
    assert response.status_code == 201, response.text
    return response.json()


def process(client, run_id, headers=BUYER):
    response = client.post(f"/api/v1/advice-runs/{run_id}/process", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def read_run(client, run_id, headers=BUYER):
    response = client.get(f"/api/v1/advice-runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def history(client, request_id, headers=BUYER):
    response = client.get(f"/api/v1/requests/{request_id}/advice-runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def edit_request(client, request_id):
    previous = get_request(client, request_id)
    body = {key: previous[key] for key in (
        "title", "sku", "quantity", "uom", "budget", "max_delivery_days", "currency")}
    body.update(expected_version=previous["version"], title=previous["title"] + " revised")
    response = client.put(f"/api/v1/requests/{request_id}", headers=BUYER, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def procurement_state(service):
    """Compare every business table; advice and audit are the only allowed writes."""
    with service.db.transaction() as session:
        return {
            table.name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                               for row in session.execute(select(table)).mappings())
            for table in Base.metadata.sorted_tables
            if table.name not in {"advice_runs", "audit_events", "alembic_version"}
        }


def stored_text(service):
    with service.db.transaction() as session:
        return json.dumps({table.name: [dict(row) for row in session.execute(select(table)).mappings()]
                           for table in Base.metadata.sorted_tables}, default=str, ensure_ascii=False)


def expire_run(service, run_id):
    from procureflow.db import AdviceRunRow
    with service.db.transaction(write=True) as session:
        row = session.get(AdviceRunRow, run_id)
        row.status = "RUNNING"
        row.started_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        row.lease_until = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


def test_pending_run_is_durable_and_creation_does_not_call_model(system, agent):
    client, service, erp = system
    req, _, _ = approved(client)
    before = procurement_state(service)
    assert history(client, req["id"]) == []
    result = create_run(client, req["id"])
    assert {
        "id", "request_id", "request_version", "status", "input_hash", "created_at",
        "started_at", "completed_at", "error_code", "output", "current", "stale_reason",
    } <= result.keys()
    assert result["request_id"] == req["id"]
    assert result["request_version"] == get_request(client, req["id"])["version"]
    assert result["status"] == "PENDING"
    assert len(result["input_hash"]) == 64
    int(result["input_hash"], 16)
    assert result["created_at"] and result["started_at"] is None and result["completed_at"] is None
    assert result["output"] is None and result["error_code"] is None
    assert "input" not in result and "idempotency_key" not in result
    assert read_run(client, result["id"]) == result
    assert history(client, req["id"]) == [result]
    assert agent.factory_calls == agent.calls == 0
    assert procurement_state(service) == before and erp.count() == 0


def test_completed_run_is_persisted_and_process_is_idempotent(system, agent):
    client, service, erp = system
    req, quote, _ = approved(client)
    before = procurement_state(service)
    pending = create_run(client, req["id"])
    result = process(client, pending["id"])
    assert result["status"] == "COMPLETED"
    assert result["started_at"] and result["completed_at"]
    assert result["error_code"] is None
    assert result["current"] is True and result["stale_reason"] is None
    assert result["output"]["advisory_only"] is True
    assert result["output"]["llm_used"] is True
    assert result["output"]["semantic_factuality_verified"] is False
    assert result["input_hash"] == pending["input_hash"]
    assert agent.comparisons[0]["request"]["version"] == pending["request_version"]
    assert agent.comparisons[0]["quotes"][0]["document_id"] == quote["document_id"]
    assert agent.sources[0]["sha256"] == quote["document_sha256"]
    assert process(client, pending["id"]) == result
    assert read_run(client, pending["id"], AUDITOR) == result
    assert history(client, req["id"], AUDITOR) == [result]
    assert agent.calls == agent.factory_calls == agent.closed == 1
    assert procurement_state(service) == before and erp.count() == 0


def test_idempotency_key_replays_original_even_after_input_changes(system, agent):
    client, _, _ = system
    req, _, _ = ready(client)
    first = create_run(client, req["id"])
    assert create_run(client, req["id"]) == first
    edit_request(client, req["id"])
    replay = create_run(client, req["id"], version=first["request_version"])
    assert replay["id"] == first["id"]
    response = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER,
                           json={"expected_version": first["request_version"] + 1,
                                 "idempotency_key": "advice-key-0001"})
    assert response.status_code == 409
    assert len(history(client, req["id"])) == 1 and agent.factory_calls == 0


def test_new_run_rejects_stale_expected_version(system, agent):
    client, _, _ = system
    req, _, _ = ready(client)
    version = get_request(client, req["id"])["version"]
    edit_request(client, req["id"])
    response = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER,
                           json={"expected_version": version, "idempotency_key": "stale-key-0001"})
    assert response.status_code == 409
    assert history(client, req["id"]) == [] and agent.factory_calls == 0


@pytest.mark.parametrize("body", [
    {"expected_version": 1, "idempotency_key": "short"},
    {"expected_version": 1, "idempotency_key": "x" * 81},
    {"expected_version": 1, "idempotency_key": "not/ascii/key"},
    {"expected_version": 1, "idempotency_key": "测试请求-key-1"},
    {"expected_version": 1, "idempotency_key": "valid-key-1 "},
    {"expected_version": True, "idempotency_key": "valid-key-1"},
    {"expected_version": "1", "idempotency_key": "valid-key-1"},
    {"expected_version": 0, "idempotency_key": "valid-key-1"},
    {"expected_version": 1, "idempotency_key": "valid-key-1", "status": "COMPLETED"},
])
def test_run_command_rejects_invalid_or_client_owned_state(system, agent, body):
    client, _, _ = system
    req = request(client)
    response = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER, json=body)
    assert response.status_code == 422, response.text
    assert history(client, req["id"]) == [] and agent.factory_calls == 0


def test_history_is_latest_twenty_and_keys_are_scoped_to_request(system, agent):
    client, _, _ = system
    req = request(client)
    created = [create_run(client, req["id"], key=f"history-key-{index:03}") for index in range(23)]
    assert [run["id"] for run in history(client, req["id"])] == [run["id"] for run in reversed(created[-20:])]
    another = request(client)
    other = create_run(client, another["id"], key="history-key-000")
    assert other["id"] != created[0]["id"]
    assert history(client, another["id"]) == [other]
    assert agent.calls == 0


def test_concurrent_creation_with_same_key_has_one_durable_run(system, agent):
    client, _, _ = system
    req, _, _ = ready(client)
    version = get_request(client, req["id"])["version"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: create_run(client, req["id"], version=version), range(4)))
    assert len({run["id"] for run in results}) == 1
    assert len(history(client, req["id"])) == 1 and agent.factory_calls == 0


def test_concurrent_processing_claims_once_and_reads_running(system, agent):
    client, service, erp = system
    req, _, _ = approved(client)
    pending = create_run(client, req["id"])
    before = procurement_state(service)
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10), "Test did not release blocked model"

    agent.effect = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process, client, pending["id"])
        try:
            assert entered.wait(5), "Model was not invoked"
            assert read_run(client, pending["id"], AUDITOR)["status"] == "RUNNING"
            second = pool.submit(process, client, pending["id"])
            assert second.result(timeout=5)["status"] == "RUNNING"
            assert agent.calls == agent.factory_calls == 1
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "COMPLETED"
    assert process(client, pending["id"])["status"] == "COMPLETED"
    assert agent.calls == 1 and procurement_state(service) == before and erp.count() == 0


@pytest.mark.parametrize("mutation", ["request", "quote_edit", "quote_confirm", "upload", "policy"])
def test_changed_inputs_before_processing_fail_closed_without_model(system, agent, monkeypatch, mutation):
    from procureflow.domain import POLICY
    client, _, _ = system
    if mutation == "quote_confirm":
        req = request(client)
        quote = upload(client, req["id"])
    else:
        req, quote, _ = ready(client)
    pending = create_run(client, req["id"])
    if mutation == "request":
        edit_request(client, req["id"])
    elif mutation == "quote_edit":
        values = {**quote["values"], "unit_price": "999.00"}
        response = client.put(f"/api/v1/quotes/{quote['id']}", headers=BUYER,
                              json={"expected_version": quote["version"], "values": values,
                                    "reason": "Supplier corrected amount"})
        assert response.status_code == 200, response.text
    elif mutation == "quote_confirm":
        # First confirmation changes request version even if values stay equal.
        response = client.post(f"/api/v1/quotes/{quote['id']}/confirm", headers=BUYER,
                               json={"expected_version": quote["version"], "acknowledge": True})
        assert response.status_code == 200, response.text
    elif mutation == "upload":
        upload(client, req["id"], "supplier-b.csv")
    else:
        monkeypatch.setitem(POLICY, "version", str(POLICY["version"]) + "-changed")
    result = process(client, pending["id"])
    assert result["status"] == "STALE"
    assert result["current"] is False and result["stale_reason"]
    assert result["error_code"] == "ADVICE_INPUT_CHANGED" and result["output"] is None
    assert process(client, pending["id"])["status"] == "STALE"
    assert agent.factory_calls == agent.calls == 0


def test_source_tamper_before_process_prevents_networking(system, agent):
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    next(service.document_dir.iterdir()).write_text("tampered source")
    result = process(client, pending["id"])
    assert result["status"] in {"FAILED", "STALE"}
    assert result["current"] is False and result["output"] is None
    assert result["error_code"] == "SOURCE_INTEGRITY_FAILED"
    assert agent.factory_calls == agent.calls == 0


def test_completed_history_becomes_noncurrent_after_edit_without_replaying(system, agent):
    client, _, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    original = process(client, pending["id"])
    edit_request(client, req["id"])
    for result in (read_run(client, pending["id"]), history(client, req["id"])[0],
                   process(client, pending["id"])):
        assert result["status"] == "COMPLETED"
        assert result["current"] is False and result["stale_reason"]
        assert result["output"] == original["output"]
        assert result["input_hash"] == original["input_hash"]
    assert agent.calls == 1


@pytest.mark.parametrize("mutation", ["request", "source", "policy"])
def test_change_during_model_call_cannot_publish_current_result(system, agent, monkeypatch, mutation):
    from procureflow.domain import POLICY
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    original_version = pending["request_version"]
    if mutation == "request":
        agent.effect = lambda: edit_request(client, req["id"])
    elif mutation == "source":
        agent.effect = lambda: next(service.document_dir.iterdir()).write_text("changed during provider call")
    else:
        agent.effect = lambda: monkeypatch.setitem(POLICY, "version", str(POLICY["version"]) + "-changed")
    result = process(client, pending["id"])
    assert result["status"] == "STALE"
    assert result["current"] is False and result["stale_reason"]
    assert result["request_version"] == original_version
    assert agent.comparisons[0]["request"]["version"] == original_version
    assert result["error_code"] == ("SOURCE_INTEGRITY_FAILED" if mutation == "source" else "ADVICE_INPUT_CHANGED")
    assert process(client, pending["id"])["status"] == "STALE" and agent.calls == 1


@pytest.mark.parametrize("failure", ["provider", "unexpected", "configuration", "unknown_code"])
def test_failure_is_terminal_safe_and_requires_explicit_new_run(system, agent, failure):
    client, service, erp = system
    req, _, _ = approved(client)
    before = procurement_state(service)
    secret = "PRIVATE-provider-body-and-reasoning-do-not-store"
    if failure == "provider":
        agent.error = DomainError("MODEL_CALL_FAILED", secret, 502)
    elif failure == "unknown_code":
        agent.error = DomainError(secret, secret, 502)
    elif failure == "configuration":
        agent.factory_error = DomainError("MODEL_NOT_CONFIGURED", secret, 503)
    else:
        agent.error = RuntimeError(secret)
    pending = create_run(client, req["id"])
    result = process(client, pending["id"])
    assert result["status"] == "FAILED" and result["error_code"]
    if failure in {"unexpected", "unknown_code"}:
        assert result["error_code"] == "ADVICE_FAILED"
    assert result["output"] is None and result["completed_at"]
    calls = agent.calls
    assert process(client, pending["id"]) == result
    assert create_run(client, req["id"])["id"] == result["id"]
    assert agent.calls == calls
    assert secret not in json.dumps(result) and secret not in stored_text(service)
    agent.error = agent.factory_error = None
    another = create_run(client, req["id"], key="explicit-retry-0002")
    assert another["id"] != pending["id"]
    assert process(client, another["id"])["status"] == "COMPLETED"
    assert procurement_state(service) == before and erp.count() == 0


def test_abandoned_running_is_derived_on_read_persisted_on_process_and_never_retried(system, agent):
    from procureflow.db import AdviceRunRow
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    expire_run(service, pending["id"])
    for result in (read_run(client, pending["id"]), history(client, req["id"])[0]):
        assert result["status"] == "INTERRUPTED"
        assert result["error_code"] == "ADVICE_INTERRUPTED"
        assert result["output"] is None
    with service.db.transaction() as session:
        assert session.get(AdviceRunRow, pending["id"]).status == "RUNNING", "GET must stay read-only"
    result = process(client, pending["id"])
    assert result["status"] == "INTERRUPTED" and result["completed_at"]
    with service.db.transaction() as session:
        assert session.get(AdviceRunRow, pending["id"]).status == "INTERRUPTED"
    assert process(client, pending["id"])["status"] == "INTERRUPTED"
    assert create_run(client, req["id"])["id"] == pending["id"]
    assert agent.factory_calls == agent.calls == 0
    retry = create_run(client, req["id"], key="explicit-interrupted-retry")
    assert process(client, retry["id"])["status"] == "COMPLETED"
    assert agent.calls == 1


def test_expired_active_processor_cannot_overwrite_interrupted_run(system, agent):
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(10), "Test did not release provider"

    agent.effect = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process, client, pending["id"])
        try:
            assert entered.wait(5)
            expire_run(service, pending["id"])
            assert process(client, pending["id"])["status"] == "INTERRUPTED"
        finally:
            release.set()
        result = first.result(timeout=5)
    assert result["status"] == "INTERRUPTED"
    assert result["output"] is None
    assert read_run(client, pending["id"])["status"] == "INTERRUPTED"
    assert agent.factory_calls == agent.calls == 1


def test_expired_lease_at_completion_cannot_publish_result(system, agent):
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    agent.effect = lambda: expire_run(service, pending["id"])
    result = process(client, pending["id"])
    assert result["status"] == "INTERRUPTED" and result["output"] is None
    assert process(client, pending["id"])["status"] == "INTERRUPTED" and agent.calls == 1


def test_pending_and_completed_runs_survive_app_and_database_reopen(system, agent):
    client, service, erp = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    # Dispose the original engine and build independent app/service/database objects.
    # Reuse the fixture URL so the PostgreSQL gate never falls back to SQLite.
    service.db.engine.dispose()
    assert agent.calls == 0
    with TestClient(create_app(service.settings, Database(service.settings.database_url), erp)) as reopened:
        assert read_run(reopened, pending["id"]) == pending
        assert create_run(reopened, req["id"])["id"] == pending["id"]
        completed = process(reopened, pending["id"])
    with TestClient(create_app(service.settings, Database(service.settings.database_url), erp)) as reopened:
        assert read_run(reopened, pending["id"]) == completed
        assert history(reopened, req["id"]) == [completed]
        assert process(reopened, pending["id"]) == completed
    assert agent.calls == 1


@pytest.mark.parametrize("headers", [AUDITOR, APPROVER])
def test_only_buyers_create_and_process_but_same_tenant_roles_can_read(system, agent, headers):
    client, _, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    assert read_run(client, pending["id"], headers)["id"] == pending["id"]
    assert history(client, req["id"], headers)[0]["id"] == pending["id"]
    assert client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=headers,
                       json={"expected_version": pending["request_version"],
                             "idempotency_key": "unauthorized-run"}).status_code == 403
    assert client.post(f"/api/v1/advice-runs/{pending['id']}/process", headers=headers).status_code == 403
    assert agent.factory_calls == 0


def test_tenant_and_auth_boundaries_cover_all_advice_routes(system, agent):
    client, _, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    routes = [
        ("get", f"/api/v1/advice-runs/{pending['id']}", None),
        ("get", f"/api/v1/requests/{req['id']}/advice-runs", None),
        ("post", f"/api/v1/advice-runs/{pending['id']}/process", None),
        ("post", f"/api/v1/requests/{req['id']}/advice-runs",
         {"expected_version": pending["request_version"], "idempotency_key": "cross-tenant-run"}),
    ]
    for method, path, body in routes:
        kwargs = {} if body is None else {"json": body}
        assert client.request(method, path, headers=OTHER, **kwargs).status_code == 404, path
        assert client.request(method, path, **kwargs).status_code == 401, path
    for method, path in [("get", "/api/v1/advice-runs/missing-run"),
                         ("post", "/api/v1/advice-runs/missing-run/process"),
                         ("get", "/api/v1/requests/missing-request/advice-runs")]:
        assert client.request(method, path, headers=BUYER).status_code == 404
    assert agent.factory_calls == 0


def test_provider_private_reasoning_not_in_durable_output_or_audit(system, monkeypatch):
    client, service, _ = system
    req, quote, _ = ready(client)
    secret = "PRIVATE-REASONING-AND-PROVIDER-EXTRAS-DO-NOT-PERSIST"
    calls = []
    evidence_id = next(item["fragment_id"] for item in quote["evidence"].values() if item.get("fragment_id"))

    def handler(http_request):
        calls.append(json.loads(http_request.content))
        if len(calls) == 1:
            message = {"content": None, "reasoning_content": secret, "provider_debug": secret,
                       "tool_calls": [
                           {"id": "call-compare", "type": "function", "function": {"name": "get_comparison", "arguments": "{}"}},
                           {"id": "call-policy", "type": "function", "function": {"name": "search_policy", "arguments": "{}"}},
                           {"id": "call-evidence", "type": "function", "function": {"name": "get_evidence", "arguments": json.dumps({"document_id": quote["document_id"]})}},
                       ]}
        else:
            message = {"content": json.dumps({"summary": "证据与比较已读取。", "evidence_ids": [evidence_id]}),
                       "reasoning_content": secret, "provider_debug": secret}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
                                        "provider_debug": secret})

    monkeypatch.setattr(ReadOnlyAgent, "from_env", staticmethod(lambda: ReadOnlyAgent(
        "http://127.0.0.1:9999/v1", "offline-test-key", "offline-model", transport=httpx.MockTransport(handler))))
    pending = create_run(client, req["id"])
    completed = process(client, pending["id"])
    assert completed["status"] == "COMPLETED", completed
    assert len(calls) == 2 and completed["output"]["model_calls"] == 2
    assert secret not in json.dumps(completed) and secret not in stored_text(service)
    assert process(client, pending["id"])["status"] == "COMPLETED" and len(calls) == 2


def test_claimed_policy_payload_stays_frozen_even_if_live_policy_changes(system, agent, monkeypatch):
    from copy import deepcopy
    from procureflow.domain import POLICY
    client, _, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    original_policy = deepcopy(POLICY)
    agent.before_tools = lambda: monkeypatch.setitem(POLICY, "version", str(POLICY["version"]) + "-changed")
    result = process(client, pending["id"])
    assert agent.policies == [original_policy], "Every tool must read the claimed immutable snapshot"
    assert result["status"] == "STALE" and result["current"] is False


def test_claimed_request_and_quote_payload_stays_frozen_while_live_request_changes(system, agent):
    client, _, _ = system
    req, quote, _ = ready(client)
    original = get_request(client, req["id"])
    pending = create_run(client, req["id"])
    agent.before_tools = lambda: edit_request(client, req["id"])
    result = process(client, pending["id"])
    comparison = agent.comparisons[0]
    assert comparison["request"]["version"] == original["version"]
    assert comparison["request"]["title"] == original["title"]
    assert comparison["quotes"][0]["document_sha256"] == quote["document_sha256"]
    assert result["status"] == "STALE" and result["current"] is False


def test_tampered_source_cannot_be_reserved_as_a_new_run(system, agent):
    client, service, _ = system
    req, _, _ = ready(client)
    version = get_request(client, req["id"])["version"]
    next(service.document_dir.iterdir()).write_text("tampered before reservation")
    response = client.post(f"/api/v1/requests/{req['id']}/advice-runs", headers=BUYER,
                           json={"expected_version": version, "idempotency_key": "tampered-reserve-key"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_INTEGRITY_FAILED"
    assert history(client, req["id"]) == [] and agent.factory_calls == 0


def test_completed_output_remains_readable_but_noncurrent_after_source_tamper(system, agent):
    client, service, _ = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    completed = process(client, pending["id"])
    next(service.document_dir.iterdir()).write_text("tampered after completion")
    result = read_run(client, pending["id"])
    assert result["status"] == "COMPLETED"
    assert result["current"] is False and result["stale_reason"] == "SOURCE_INTEGRITY_FAILED"
    assert result["output"] == completed["output"] and agent.calls == 1


def test_reservation_holds_request_lock_until_snapshot_and_receipt_are_committed(system, agent, monkeypatch):
    from concurrent.futures import TimeoutError
    from procureflow.advice import AdviceService
    client, service, _ = system
    req, _, _ = ready(client)
    version = get_request(client, req["id"])["version"]
    entered, release, edit_entered = Event(), Event(), Event()
    original_capture = AdviceService._capture
    original_update = service.update_request

    def blocked_capture(self, session, principal, row):
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "Test did not release snapshot capture"
        return original_capture(self, session, principal, row)

    def observed_update(*args, **kwargs):
        edit_entered.set()
        return original_update(*args, **kwargs)

    monkeypatch.setattr(AdviceService, "_capture", blocked_capture)
    monkeypatch.setattr(service, "update_request", observed_update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservation = pool.submit(create_run, client, req["id"], version=version)
        try:
            assert entered.wait(5)
            edit = pool.submit(edit_request, client, req["id"])
            assert edit_entered.wait(5)
            with pytest.raises(TimeoutError):
                edit.result(timeout=0.25)
        finally:
            release.set()
        pending = reservation.result(timeout=5)
        changed = edit.result(timeout=5)
    assert pending["request_version"] == version
    assert changed["version"] == version + 1
    assert process(client, pending["id"])["status"] == "STALE"
    assert agent.factory_calls == 0


def test_crashed_model_claim_survives_reopen_without_automatic_retry(system, agent):
    from procureflow.advice import AdviceService
    from procureflow.contracts import Principal

    class SimulatedProcessDeath(BaseException):
        pass

    client, service, erp = system
    req, _, _ = ready(client)
    pending = create_run(client, req["id"])
    agent.error = SimulatedProcessDeath("abrupt termination after provider invocation")
    with pytest.raises(SimulatedProcessDeath):
        AdviceService(service).process(
            Principal(user_id="buyer-01", tenant_id="demo", role="buyer"), pending["id"], agent.factory)
    service.db.engine.dispose()
    second_app = create_app(service.settings, Database(service.settings.database_url), erp)
    with TestClient(second_app) as reopened:
        assert read_run(reopened, pending["id"])["status"] == "RUNNING"
        assert process(reopened, pending["id"])["status"] == "RUNNING"
        assert agent.calls == agent.factory_calls == 1
        expire_run(second_app.state.service, pending["id"])
        assert process(reopened, pending["id"])["status"] == "INTERRUPTED"
        assert agent.calls == agent.factory_calls == 1
        agent.error = None
        retry = create_run(reopened, req["id"], key="explicit-crash-retry")
        assert process(reopened, retry["id"])["status"] == "COMPLETED"
        assert agent.calls == agent.factory_calls == 2
