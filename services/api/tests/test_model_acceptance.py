"""Synthetic acceptance tests. MockTransport and loopback HTTP are not live vendors."""
from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("model_acceptance", ROOT / "scripts/verify_model_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class NoReads(dict):
    def get(self, *args, **kwargs):
        raise AssertionError("Environment read before explicit opt-in")

    def __getitem__(self, key):
        raise AssertionError("Environment read before explicit opt-in")


def test_default_blocks_before_environment_or_worker_access(monkeypatch):
    monkeypatch.setattr(acceptance.os, "environ", NoReads())
    monkeypatch.setattr(acceptance.subprocess, "run", lambda *a, **k: pytest.fail("Worker must not start"))
    result, code = acceptance.run_acceptance()
    assert code == 2
    assert result["reason"] == "EXPLICIT_NETWORK_OPT_IN_REQUIRED"
    assert result["network_attempted"] is False and result["provider_http_requests"] == 0


@pytest.mark.parametrize("overrides", [
    {"max_model_calls": 0}, {"max_model_calls": 7}, {"max_model_calls": True},
    {"max_tool_calls": 13}, {"max_reported_tokens": 1599}, {"max_reported_tokens": 16001},
    {"max_wall_seconds": 61}, {"max_wall_seconds": 0}, {"unknown_budget": 1},
])
def test_invalid_budgets_block_before_secret_access(monkeypatch, overrides):
    monkeypatch.setattr(acceptance.os, "environ", NoReads())
    result, code = acceptance.run_acceptance(allow_network=True, **overrides)
    assert code == 2 and result["reason"] == "INVALID_BUDGET"


def test_fixture_and_live_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(acceptance.os, "environ", NoReads())
    result, code = acceptance.run_acceptance(True, fixture=True)
    assert code == 2 and result["reason"] == "CHOOSE_FIXTURE_OR_NETWORK"


def test_live_requires_configuration_but_does_not_read_app_secrets(monkeypatch):
    class OnlyModel(dict):
        def get(self, key, default=None):
            assert key in {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_THINKING_MODE"}
            return default
    monkeypatch.setattr(acceptance.os, "environ", OnlyModel())
    result, code = acceptance.run_acceptance(True)
    assert code == 2 and result["reason"] == "MODEL_CONFIGURATION_REQUIRED"


def test_fixture_isolated_before_app_import_and_reports_no_prose(tmp_path):
    # Poison every normal deployment input. None may reach the disposable worker.
    poison = "PRIVATE_CONFIG_MUST_NOT_APPEAR"
    env = {"PF_MODE": "private", "PF_DATABASE_URL": "postgresql://MUST_NOT_CONNECT",
           "PF_DATA_DIR": str(tmp_path / "MUST_NOT_CREATE"), "PF_AUTH_TOKENS": poison,
           "PF_ERP_MODE": "erpnext", "ERP_ALLOW_DRAFT_WRITES": "true", "ERP_API_KEY": poison,
           "ERP_API_SECRET": poison, "ERP_BASE_URL": "https://must-not-connect.invalid",
           "LLM_API_KEY": poison, "LLM_BASE_URL": "https://must-not-connect.invalid", "LLM_MODEL": poison,
           "LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true", "LANGSMITH_API_KEY": poison}
    path = tmp_path / "report.json"
    completed = subprocess.run([sys.executable, str(ROOT / "scripts/verify_model_acceptance.py"),
        "--fixture", "--output", str(path)], env=env, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(path.read_text())
    assert result == json.loads(completed.stdout)
    assert result["status"] == "passed"
    assert result["evaluation_kind"] == "offline_protocol_fixture"
    assert result["network_attempted"] is False
    assert result["model_calls"] == 3 and result["provider_http_requests"] == 3 and result["tool_calls"] == 3
    assert result["usage"] == {"prompt_tokens": 180, "completion_tokens": 60, "total_tokens": 240}
    assert result["graph_execution_verified"] and result["graph_node_count"] == 6
    for key in ("durable_receipt_verified", "replay_prevented", "business_state_unchanged",
                "evidence_read_verified", "unknown_shipping_preserved", "advisory_only"):
        assert result[key], key
    assert not result["live_provider_protocol_verified"] and not result["quality_acceptance_verified"]
    assert result["cost"] is None and result["task_success_rate"] is None
    assert result["semantic_factuality_verified"] is False
    serialized = completed.stdout + completed.stderr + path.read_text()
    for forbidden in (poison, "PRIVATE_FIXTURE_CONTINUATION", "summary", "messages", "reasoning_content",
                      "SYNTHETIC-A", "synthetic-fixture-token", "运费未知"):
        assert forbidden not in serialized
    assert not (tmp_path / "MUST_NOT_CREATE").exists()


@pytest.mark.parametrize("budget, value, expected_requests", [
    ("max_model_calls", 2, 2), ("max_tool_calls", 1, 1),
])
def test_fixture_stops_at_budget_with_durable_failure_and_no_replay(budget, value, expected_requests):
    result, code = acceptance.run_acceptance(fixture=True, **{budget: value})
    assert code == 1 and result["reason"] == "BUDGET_EXCEEDED"
    assert result["provider_http_requests"] == expected_requests
    assert result["replay_prevented"] and result["durable_receipt_verified"]
    assert result["business_state_unchanged"] and result["external_business_writes"] == 0


def test_worker_deadline_does_not_retry_or_expose_partial_output(monkeypatch):
    calls = []
    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("safe command", kwargs["timeout"], output="PRIVATE_PROVIDER_BODY")
    monkeypatch.setattr(acceptance.subprocess, "run", timeout)
    result, code = acceptance.run_acceptance(fixture=True, max_wall_seconds=1)
    assert code == 1 and result["reason"] == "WORKER_WALL_BUDGET_EXCEEDED"
    assert len(calls) == 1 and calls[0]["timeout"] == 21
    assert result["network_attempted"] is None and result["provider_http_requests"] is None
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps(result)
    assert set(calls[0]["env"]) == set(acceptance._sandbox_environment("/tmp/example"))
    assert "LLM_API_KEY" not in calls[0]["env"]


@pytest.mark.parametrize("field, value", [
    ("reason", "PRIVATE_PROVIDER_ERROR"), ("usage", {"private": "PRIVATE_PROVIDER_ERROR"}),
    ("graph_node_count", 1000000), ("advisory_only", "PRIVATE_PROVIDER_ERROR"),
])
def test_worker_export_schema_rejects_arbitrary_metadata(monkeypatch, field, value):
    def invalid_report(*args, **kwargs):
        report = acceptance._report("offline_protocol_fixture")
        report.update(status="failed", reason="ACCEPTANCE_FAILED", worker_wall_limit_seconds=55)
        report[field] = value
        return subprocess.CompletedProcess(args[0], 1, stdout=json.dumps(report))
    monkeypatch.setattr(acceptance.subprocess, "run", invalid_report)
    result, code = acceptance.run_acceptance(fixture=True)
    assert code == 1 and result["reason"] == "ACCEPTANCE_WORKER_FAILED"
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(result)


@pytest.mark.parametrize("mode, expected_code, expected_reason, count", [
    ("success", 0, None, 3),
    ("http-error", 1, "MODEL_CALL_FAILED", 1),
    ("missing-usage", 1, "MODEL_USAGE_REQUIRED", 1),
    ("unsafe-tool", 1, "TOOL_POLICY_DENIED", 1),
    ("over-tokens", 1, "BUDGET_EXCEEDED", 1),
    ("missing-evidence", 1, "EVIDENCE_NOT_FOUND", 3),
])
def test_loopback_provider_protocol_is_not_live_vendor_acceptance(monkeypatch, mode, expected_code, expected_reason, count):
    seen, failures = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                assert self.headers["Authorization"] == "Bearer SYNTHETIC_LOCAL_KEY"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["model"] == "synthetic-local-model"
                assert body["thinking"] == {"type": "enabled"}
                assert body["max_tokens"] == 1600
                assert body["response_format"] == {"type": "json_object"}
                assert {tool["function"]["name"] for tool in body["tools"]} == {
                    "get_comparison", "search_policy", "get_evidence"}
                assert "SYNTHETIC_LOCAL_KEY" not in json.dumps(body)
                seen.append(body)
                if len(seen) > 1:
                    assert any(message.get("reasoning_content") == "PRIVATE_FIXTURE_CONTINUATION"
                               for message in body["messages"])
                response = acceptance._fixture_message(body)
                if mode == "http-error":
                    self.send_response(503)
                    response = {"error": "PRIVATE_PROVIDER_ERROR"}
                else:
                    self.send_response(200)
                if mode == "missing-usage":
                    response.pop("usage")
                elif mode == "unsafe-tool":
                    response["choices"][0]["message"] = {"tool_calls": [{"id": "bad", "function": {
                        "name": "approve_purchase", "arguments": "{}"}}]}
                elif mode == "over-tokens":
                    response["usage"] = {"prompt_tokens": 16000, "completion_tokens": 1600}
                elif mode == "missing-evidence" and len(seen) == 3:
                    response["choices"][0]["message"] = {"content": json.dumps({
                        "summary": "PRIVATE_PROVIDER_PROSE", "evidence_ids": ["not-a-source"]})}
                payload = json.dumps(response).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as error:
                failures.append(str(error))
                self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LLM_API_KEY", "SYNTHETIC_LOCAL_KEY")
    monkeypatch.setenv("LLM_MODEL", "synthetic-local-model")
    monkeypatch.setenv("LLM_THINKING_MODE", "enabled")
    try:
        result, code = acceptance.run_acceptance(allow_network=True)
        assert not failures, failures
        assert code == expected_code, result
        assert result["reason"] == expected_reason
        assert len(seen) == result["provider_http_requests"] == count
        assert result["evaluation_kind"] == "loopback_protocol_fixture"
        assert result["network_attempted"] is True
        assert result["live_provider_protocol_verified"] is False
        assert result["quality_acceptance_verified"] is False
        assert result["durable_receipt_verified"] and result["replay_prevented"]
        assert result["business_state_unchanged"] and result["external_business_writes"] == 0
        serialized = json.dumps(result)
        for secret in ("SYNTHETIC_LOCAL_KEY", "PRIVATE_FIXTURE_CONTINUATION", "PRIVATE_PROVIDER_ERROR",
                       "PRIVATE_PROVIDER_PROSE", "not-a-source", "synthetic-local-model"):
            assert secret not in serialized
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
