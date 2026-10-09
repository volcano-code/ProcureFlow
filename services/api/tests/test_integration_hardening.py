from __future__ import annotations
import json
import importlib.util
from pathlib import Path
import httpx
import pytest
from procureflow.agent import ReadOnlyAgent, normalized_usage
from procureflow.outbound import validate_endpoint
from procureflow.erp import ERPNextClient, ERPRejected, ERPUnknown
from procureflow.errors import DomainError


def response(message, usage=None):
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})


def call_message(call_id="one", **extra):
    return {"content": None, "tool_calls": [{"id": call_id, "type": "function",
        "function": {"name": "get_comparison", "arguments": "{}"}}], **extra}


def final_message():
    return {"content": json.dumps({"summary": "运费未知，不可执行。", "evidence_ids": []})}


def test_reasoning_continuation_is_passed_to_provider_but_never_audited():
    requests, audit = [], []
    continuation = "PRIVATE PROVIDER CONTINUATION"
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "enabled"}
        if len(requests) == 1:
            return response(call_message(reasoning_content=continuation))
        assert payload["messages"][-2]["reasoning_content"] == continuation
        return response(final_message())
    agent = ReadOnlyAgent("https://model.test/v1", "synthetic-key", "test", thinking_mode="enabled", transport=httpx.MockTransport(handle))
    try:
        result = agent.run(lambda *_: {"shipping_cost": None}, set(), observer=audit.append)
        assert continuation not in json.dumps(result) and continuation not in json.dumps(audit)
        assert result["usage"] is None and result["cost"] is None
    finally:
        agent.client.close()


def test_provider_usage_is_allowlisted_and_aggregated():
    n = 0
    def handle(_):
        nonlocal n
        n += 1
        return response(call_message() if n == 1 else final_message(), {
            "prompt_tokens": 15, "completion_tokens": 6, "total_tokens": 21,
            "sensitive_provider_metadata": "DO_NOT_STORE"})
    agent = ReadOnlyAgent("https://model.test", "key", "test", transport=httpx.MockTransport(handle))
    try:
        result = agent.run(lambda *_: {}, set())
        assert result["usage"] == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}
        assert result["usage_complete"] and "DO_NOT_STORE" not in json.dumps(result)
    finally:
        agent.client.close()


@pytest.mark.parametrize("usage", [None, [], {}, {"prompt_tokens": -1, "completion_tokens": 1},
    {"prompt_tokens": True, "completion_tokens": 1}, {"prompt_tokens": "1", "completion_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}])
def test_malformed_usage_remains_unknown(usage):
    assert normalized_usage(usage) is None


def test_cross_round_duplicate_id_fails_before_second_tool_invocation():
    invoked = []
    agent = ReadOnlyAgent("https://model.test", "key", "test", transport=httpx.MockTransport(lambda _: response(call_message())))
    try:
        with pytest.raises(DomainError) as caught:
            agent.run(lambda *args: invoked.append(args) or {}, set())
        assert caught.value.code == "MODEL_PROTOCOL_INVALID" and len(invoked) == 1
    finally:
        agent.client.close()


@pytest.mark.parametrize("strict,usage,code", [
    (True, None, "MODEL_USAGE_REQUIRED"),
    (False, {"prompt_tokens": 1600, "completion_tokens": 2}, "BUDGET_EXCEEDED")])
def test_usage_budget_prevents_followup_tool(strict, usage, code):
    agent = ReadOnlyAgent("https://model.test", "key", "test", max_reported_tokens=1600,
        require_usage=strict, transport=httpx.MockTransport(lambda _: response(call_message(), usage)))
    try:
        with pytest.raises(DomainError) as caught:
            agent.run(lambda *_: pytest.fail("No tool invocation allowed"), set())
        assert caught.value.code == code
    finally:
        agent.client.close()


def test_wall_budget_checked_after_each_tool(monkeypatch):
    import procureflow.agent as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    message = call_message()
    message["tool_calls"].append({"id": "two", "function": {"name": "search_policy", "arguments": "{}"}})
    agent = ReadOnlyAgent("https://model.test", "key", "test", max_wall_seconds=1,
                         transport=httpx.MockTransport(lambda _: response(message)))
    invoked = []
    def invoke(name, _):
        invoked.append(name)
        clock[0] = 2
        return {}
    try:
        with pytest.raises(DomainError) as caught:
            agent.run(invoke, set())
        assert caught.value.code == "BUDGET_EXCEEDED" and invoked == ["get_comparison"]
    finally:
        agent.client.close()


@pytest.mark.parametrize("url", ["http://erp.company.test", "http://192.168.1.2", "ftp://example.com",
    "https://user:secret@example.com", "https://example.com?token=x", "https://example.com/#x",
    "https://example.com:bad", "https://example.com\\@evil.test", "https://bad host.test"])
def test_integration_urls_fail_before_network(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


@pytest.mark.parametrize("url", ["https://provider.test/v1", "http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:9000"])
def test_explicit_tls_or_loopback_endpoints(url):
    assert validate_endpoint(url).endswith("/")


class OversizedStream(httpx.SyncByteStream):
    def __init__(self):
        self.reads = 0
        self.closed = False
    def __iter__(self):
        for _ in range(1000):
            self.reads += 1
            yield b"x" * 8192
    def close(self):
        self.closed = True


@pytest.mark.parametrize("target", ["model", "erp"])
def test_response_limit_stops_stream_early_and_closes_connection(target):
    stream = OversizedStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    if target == "model":
        client = ReadOnlyAgent("https://model.test", "key", "test", transport=transport)
        action = lambda: client.run(lambda *_: {}, set())
        error_type, code = DomainError, "MODEL_RESPONSE_LIMIT"
    else:
        client = ERPNextClient("https://erp.test", "key", "secret", "Test", transport=transport)
        action = client.suppliers
        error_type, code = ERPUnknown, "ERP_RESPONSE_LIMIT"
    try:
        with pytest.raises(error_type) as caught:
            action()
        assert (caught.value.code if target == "model" else str(caught.value)) == code
        assert 0 < stream.reads < 1000 and stream.closed
    finally:
        client.client.close()


@pytest.mark.parametrize("method,path", [("POST", "api/resource/Supplier Quotation"), ("DELETE", "api/resource/Supplier Quotation/X")])
def test_erp_readonly_client_rejects_lowlevel_writes(method, path):
    client = ERPNextClient("https://erp.test", "key", "secret", "Test",
        transport=httpx.MockTransport(lambda _: pytest.fail("Must reject before network")))
    try:
        with pytest.raises(ERPRejected, match="WRITES_DISABLED"):
            client._call(method, path)
    finally:
        client.client.close()


def test_erp_identity_mismatch_fails_with_only_get():
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"message": "Administrator"})
    client = ERPNextClient("https://erp.test", "key", "secret", "Test", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ERPRejected, match="IDENTITY_MISMATCH"):
            client.preflight(expected_user="integration@test.invalid")
        assert len(seen) == 1 and seen[0].method == "GET"
    finally:
        client.client.close()


def load_probe():
    path = Path(__file__).resolve().parents[3] / "scripts/verify_integrations.py"
    spec = importlib.util.spec_from_file_location("probe_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("target", ["model", "erp"])
def test_probe_defaults_blocked_without_constructing_clients(monkeypatch, target):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("No client without opt-in"))
    result, code = load_probe().run_probe(target, False)
    assert code == 2 and result["status"] == "blocked" and not result["network_attempted"]


def test_probe_refuses_demo_before_network(monkeypatch):
    monkeypatch.setenv("PF_MODE", "demo")
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("No client in demo"))
    result, code = load_probe().run_probe("model", True)
    assert code == 2 and result["reason"] == "PRIVATE_SANDBOX_CONFIGURATION_REQUIRED"


def test_probe_does_not_dump_bad_private_config_or_credentials(monkeypatch):
    values = {"PF_MODE": "private", "PF_INTEGRATION_ENVIRONMENT": "sandbox", "PF_ERP_MODE": "mock",
        "PF_AUTH_TOKENS": "SECRET-MALFORMED-JSON", "LLM_API_KEY": "SECRET-KEY",
        "LLM_MODEL": "test", "LLM_BASE_URL": "https://model.test"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result, code = load_probe().run_probe("model", True)
    assert code == 1 and "SECRET" not in json.dumps(result) and not result["network_attempted"]
