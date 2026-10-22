from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest
from ag_ui.core import Event
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import TypeAdapter
from sqlalchemy import func, select

from integrations.protocols.agui import install_agui
from integrations.protocols.readonly import (BridgeError, MAX_RESPONSE_BYTES, REGISTRY,
    ReadOnlyGateway, api_origin, function_tools, read_token)
from procureflow.app import create_app
from procureflow.config import Settings
from procureflow.db import ApprovalRow, Database, OperationRow, QuoteRow
from procureflow.erp import MockERP

ROOT = Path(__file__).resolve().parents[3]
BUYER = {"Authorization": "Bearer demo-buyer"}
AUDITOR = {"Authorization": "Bearer demo-auditor"}
OTHER = {"Authorization": "Bearer demo-other-tenant"}


def new_request(client, headers=BUYER):
    response = client.post("/api/v1/requests", headers=headers, json={"title": "Protocol synthetic request",
        "sku": "STAND-01", "quantity": "20", "budget": "30000.00", "max_delivery_days": 14})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def system(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}", mode="demo", erp_mode="mock")
    db = Database(settings.database_url, create_schema=True)
    app = install_agui(create_app(settings, db, MockERP(tmp_path / "erp.sqlite3")), poll_count=2, poll_seconds=0)
    with TestClient(app) as client:
        yield client, app, db


def response(data, status=200, **headers):
    return httpx.Response(status, headers={"content-type": "application/json", **headers},
        stream=httpx.ByteStream(json.dumps(data).encode()))


def run_call(handler, name="get_request", arguments=None):
    async def exercise():
        gateway = ReadOnlyGateway("http://127.0.0.1:8000", "demo-auditor", "req_bound", transport=httpx.MockTransport(handler))
        try:
            return await gateway.call(name, arguments)
        finally:
            await gateway.aclose()
    return asyncio.run(exercise())


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://10.0.0.1", "ftp://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=y", "https://example.com#frag", "http://127.0.0.1:0", " https://example.com", "https://example.com./", "file:///tmp/token"])
def test_origin_restrictions(origin):
    with pytest.raises(BridgeError, match="INVALID_API_ORIGIN"):
        api_origin(origin)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://[::1]:8000", "https://procurement.example.com"])
def test_valid_operator_origins(origin):
    assert api_origin(origin) == origin


@pytest.mark.parametrize("args", [{"tenant_id": "other"}, {"role": "approver"}, {"request_id": "other"}, {"token": "private-token"}, {"url": "https://example.org"}, [], "get_request"])
def test_extra_arguments_rejected_before_network(args):
    calls = []
    with pytest.raises(BridgeError, match="INVALID_ARGUMENTS"):
        run_call(lambda request: calls.append(request), arguments=args)
    assert not calls


@pytest.mark.parametrize("document", ["../secret", "%2e%2e", "a/b", "a?role=admin", "", 1])
def test_document_identifier_injection(document):
    with pytest.raises(BridgeError, match="INVALID_ARGUMENTS"):
        run_call(lambda request: pytest.fail("network before validation"), "get_evidence", {"document_id": document})


def test_registry_and_only_get_dispatch():
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer demo-auditor"
        if request.url.path.endswith("/quotes"):
            return response([{"request_id": "req_bound", "document_id": "doc_bound"}])
        if request.url.path.endswith("/evidence"):
            return response({"fragments": [{"text": "Untrusted vendor source"}]})
        if request.url.path == "/api/v1/requests/req_bound":
            return response({"id": "req_bound"})
        return response([])
    for tool in REGISTRY:
        args = {"document_id": "doc_bound"} if tool == "get_evidence" else {}
        assert run_call(handler, tool, args)["read_only"] is True
    assert seen and {m for m, _ in seen} == {"GET"}
    assert {t["function"]["name"] for t in function_tools()} == set(REGISTRY)
    assert all(t["function"]["parameters"]["additionalProperties"] is False for t in function_tools())


def test_evidence_cannot_escape_selected_request():
    seen = []
    def handler(request):
        seen.append(request.url.path)
        return response([]) if request.url.path.endswith("/quotes") else response({"id": "req_bound"})
    with pytest.raises(BridgeError, match="DOCUMENT_OUT_OF_SCOPE"):
        run_call(handler, "get_evidence", {"document_id": "doc_elsewhere"})
    assert all(not path.endswith("/evidence") for path in seen)


@pytest.mark.parametrize("name", ["approve", "execute", "process_candidate", "get_request/../../execute"])
def test_write_tools_absent(name):
    with pytest.raises(BridgeError, match="UNKNOWN_TOOL"):
        run_call(lambda request: pytest.fail("unknown tool must not network"), name)


@pytest.mark.parametrize("status,code", [(401,"UNAUTHENTICATED"),(403,"FORBIDDEN"),(404,"NOT_FOUND"),(429,"RATE_LIMITED"),(500,"UPSTREAM_REJECTED"),(302,"UPSTREAM_REJECTED")])
def test_upstream_errors_redacted_no_redirect(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return response({"secret": "must-never-be-reflected"}, status, location="https://other.example/steal")
    with pytest.raises(BridgeError) as error:
        run_call(handler)
    assert error.value.code == code
    assert "secret" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("body", [b'{"id":"req_bound","id":"other"}', b'{"value":NaN}', b'not json', b'\xff'])
def test_malformed_upstream_json(body):
    with pytest.raises(BridgeError, match="UPSTREAM_INVALID"):
        run_call(lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body)))


def test_oversized_upstream_response():
    with pytest.raises(BridgeError, match="UPSTREAM_SIZE"):
        run_call(lambda request: response({"huge": "x" * MAX_RESPONSE_BYTES}))


def test_upstream_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("token=never-echo", request=request)
    with pytest.raises(BridgeError, match="UPSTREAM_TIMEOUT"):
        run_call(handler)


def test_token_file_private_and_not_symlink(tmp_path):
    token = tmp_path / "reader.token"
    token.write_text("demo-auditor\n")
    token.chmod(0o600)
    assert read_token(str(token)) == "demo-auditor"
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(BridgeError, match="INVALID_TOKEN_FILE"):
        read_token(str(link))
    token.chmod(0o644)
    with pytest.raises(BridgeError, match="INVALID_TOKEN_FILE"):
        read_token(str(token))


def decode_events(text):
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            # The official SDK union validates the actual wire format.
            event = TypeAdapter(Event).validate_json(line[6:])
            events.append(event.model_dump(by_alias=True, mode="json", exclude_none=True))
    return events


def counts(db):
    with db.transaction() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in (QuoteRow, ApprovalRow, OperationRow))


def test_agui_official_models_and_no_side_effects(system):
    client, app, db = system
    req = new_request(client)
    before = counts(db)
    result = client.get(f"/api/v1/requests/{req['id']}/ag-ui/events", headers=AUDITOR)
    assert result.status_code == 200
    assert "no-transform" in result.headers["cache-control"]
    events = decode_events(result.text)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[0]["threadId"] == req["id"]
    assert events[1]["type"] == "STATE_SNAPSHOT"
    assert events[1]["snapshot"]["writes_enabled"] is False
    audit = [e for e in events if e["type"] == "CUSTOM"]
    assert audit and audit[0]["name"] == "procureflow.audit"
    assert set(audit[0]["value"]) == {"audit_id", "request_id", "kind", "created_at"}
    assert events[-1]["type"] == "RUN_FINISHED" and events[-1]["result"]["observation_only"] is True
    assert "reasoning_content" not in result.text and '"payload"' not in result.text
    assert counts(db) == before == (0, 0, 0)
    assert client.get(f"/api/v1/requests/{req['id']}", headers=BUYER).json()["version"] == req["version"]


@pytest.mark.parametrize("headers,status", [({},401), ({"X-Role":"approver"},401), (OTHER,404)])
def test_agui_denies_before_stream(system, headers, status):
    client, _, _ = system
    rid = new_request(client)["id"]
    result = client.get(f"/api/v1/requests/{rid}/ag-ui/events", headers=headers)
    assert result.status_code == status and "RUN_STARTED" not in result.text


@pytest.mark.parametrize("cursor", ["-1", "abc", "1.0", "01", "9223372036854775808"])
def test_agui_rejects_invalid_resume_cursor(system, cursor):
    client, _, _ = system
    rid = new_request(client)["id"]
    result = client.get(f"/api/v1/requests/{rid}/ag-ui/events", headers={**AUDITOR, "Last-Event-ID": cursor})
    assert result.status_code == 422


def test_agui_resume_does_not_replay_old_audit(system):
    client, _, _ = system
    rid = new_request(client)["id"]
    url = f"/api/v1/requests/{rid}/ag-ui/events"
    initial = decode_events(client.get(url, headers=AUDITOR).text)
    cursor = max(e["value"]["audit_id"] for e in initial if e["type"] == "CUSTOM")
    resumed = decode_events(client.get(url, headers={**AUDITOR, "Last-Event-ID": str(cursor)}).text)
    assert not [e for e in resumed if e["type"] == "CUSTOM"]
    assert resumed[1]["type"] == "STATE_SNAPSHOT"


def test_agui_durable_pilot_revocation_stops_stream(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'pilot.sqlite3'}", mode="pilot", erp_mode="mock")
    db = Database(settings.database_url, create_schema=True)
    app = install_agui(create_app(settings, db, MockERP(tmp_path / "erp.sqlite3")), poll_count=2, poll_seconds=0)
    ids = app.state.identities
    ids.create_tenant("protocol-test")
    tokens = {}
    for role in ("buyer", "auditor"):
        ids.set_membership("protocol-test", role, role)
        tokens[role] = ids.login(ids.issue_invite("protocol-test", role)["credential"])["token"]
    original = app.state.service.events
    calls = []
    def revoke_after_read(principal, rid, after=0):
        batch = original(principal, rid, after)
        calls.append(True)
        ids.revoke_user_sessions("protocol-test", "auditor")
        return batch
    app.state.service.events = revoke_after_read
    with TestClient(app) as client:
        rid = new_request(client, {"Authorization": "Bearer " + tokens["buyer"]})["id"]
        result = client.get(f"/api/v1/requests/{rid}/ag-ui/events", headers={"Authorization": "Bearer " + tokens["auditor"]})
        events = decode_events(result.text)
        assert len(calls) == 1
        assert events[-1]["type"] == "RUN_ERROR" and events[-1]["code"] == "UNAUTHENTICATED"
        assert not any(e["type"] == "RUN_FINISHED" for e in events)
        assert tokens["auditor"] not in result.text


@pytest.fixture
def live_api(tmp_path):
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    data = tmp_path / "api-data"
    data.mkdir()
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG") if k in os.environ}
    env.update(PYTHONPATH=str(ROOT / "services/api") + os.pathsep + str(ROOT),
        PF_DATA_DIR=str(data), PF_MODE="demo", PF_ERP_MODE="mock", ERP_ALLOW_DRAFT_WRITES="false")
    log = (tmp_path / "api.log").open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "integrations.protocols.agui:create_protocol_app", "--factory", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    origin = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert proc.poll() is None, "API process exited"
                time.sleep(0.1)
            else:
                pytest.fail("API startup deadline")
            req = new_request(client)
            content = (ROOT / "evals/fixtures/supplier-a.txt").read_bytes()
            q = client.post(f"/api/v1/requests/{req['id']}/documents", headers=BUYER,
                files={"file": ("supplier-a.txt", content)})
            assert q.status_code == 201, q.text
            yield origin, req["id"], q.json()["document_id"], data
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()


def test_official_mcp_client_real_subprocess(live_api, tmp_path):
    origin, rid, did, data = live_api
    credential = tmp_path / "stdio-reader.token"
    credential.write_text("demo-auditor")
    credential.chmod(0o600)
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "integrations.protocols.mcp_server"], cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT), "PF_READ_API_ORIGIN": origin,
                 "PF_READ_REQUEST_ID": rid, "PF_READ_TOKEN_FILE": str(credential)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {t.name for t in tools} == set(REGISTRY)
                assert all(t.annotations.readOnlyHint for t in tools)
                for t in tools:
                    assert t.inputSchema == REGISTRY[t.name].schema()
                    args = {"document_id": did} if t.name == "get_evidence" else {}
                    result = await session.call_tool(t.name, args)
                    assert not result.isError, result
                    assert result.structuredContent["scope_request_id"] == rid
                    assert result.structuredContent["read_only"] is True
                assert (await session.call_tool("approve", {})).isError
                denied = await session.call_tool("get_request", {"tenant_id": "other"})
                assert denied.isError and "other" not in denied.content[0].text
                assert (await session.call_tool("get_evidence", {"document_id": "doc_unknown"})).isError
        # A second authenticated client from another tenant may not read rid.
        credential.write_text("demo-other-tenant")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                denied = await session.call_tool("get_request", {})
                assert denied.isError and "NOT_FOUND" in denied.content[0].text
    asyncio.run(asyncio.wait_for(exercise(), timeout=45))
    with httpx.Client(base_url=origin, trust_env=False) as client:
        assert client.get(f"/api/v1/requests/{rid}/approvals", headers=AUDITOR).json() == []
        quotes = client.get(f"/api/v1/requests/{rid}/quotes", headers=AUDITOR).json()
        assert len(quotes) == 1 and quotes[0]["confirmed_by"] is None
    # Independently inspect the real application ledger, never create a new DB
    # accidentally while checking for absence of writes.
    db_file = data / "procureflow.sqlite3"
    assert db_file.is_file()
    with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("select count(*) from external_operations").fetchone()[0] == 0
        assert conn.execute("select count(*) from approvals").fetchone()[0] == 0


def test_agui_real_http_official_consumer(live_api):
    origin, rid, _, _ = live_api
    events = []
    with httpx.Client(base_url=origin, trust_env=False, timeout=12) as client:
        with client.stream("GET", f"/api/v1/requests/{rid}/ag-ui/events", headers=AUDITOR) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    event = TypeAdapter(Event).validate_json(line[6:]).model_dump(by_alias=True, mode="json")
                    events.append(event)
                    if event["type"] == "CUSTOM":
                        break
    assert [e["type"] for e in events[:2]] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert events[-1]["type"] == "CUSTOM" and events[-1]["value"]["request_id"] == rid
