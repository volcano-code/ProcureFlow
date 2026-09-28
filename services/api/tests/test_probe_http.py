"""Actual loopback HTTP/subprocess tests. These servers emulate providers, not live integrations."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import pytest


@pytest.mark.parametrize("target", ["model", "erp"])
def test_probe_against_loopback_protocol_fixture(tmp_path, target):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def reply(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            seen.append(("POST", self.path))
            assert self.path == "/v1/chat/completions"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "LOCAL_SECRET" not in json.dumps(body)
            if len(seen) == 1:
                message = {"reasoning_content": "fixture-private-continuation", "tool_calls": [
                    {"id": "read-comparison", "function": {"name": "get_comparison", "arguments": "{}"}},
                    {"id": "read-policy", "function": {"name": "search_policy", "arguments": "{}"}}]}
            else:
                assert body["messages"][-3]["reasoning_content"] == "fixture-private-continuation"
                message = {"content": json.dumps({"summary": "运费未知，不允许采购", "evidence_ids": ["probe:price"]})}
            self.reply({"choices": [{"message": message}], "usage": {"prompt_tokens": 30, "completion_tokens": 10}})
        def do_GET(self):
            seen.append(("GET", self.path))
            from urllib.parse import urlparse, parse_qs, unquote
            url = urlparse(self.path)
            path = unquote(url.path)
            if path.endswith("get_logged_user"):
                self.reply({"message": "integration@test.invalid"})
            elif path.endswith("Custom Field"):
                field = json.loads(parse_qs(url.query)["filters"][0])[-1][-1]
                self.reply({"data": [{"fieldname": field, "unique": 1, "fieldtype": "Data"}]})
            elif "/Company/" in path:
                self.reply({"data": {"name": "SyntheticCompany"}})
            elif path.endswith("Supplier"):
                self.reply({"data": []})
            else:
                self.send_error(404)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[3]
    url = f"http://127.0.0.1:{server.server_port}"
    env = {**os.environ, "PF_MODE": "private", "PF_INTEGRATION_ENVIRONMENT": "sandbox",
        "PF_AUTH_TOKENS": json.dumps({"x" * 40: {"user_id": "test-user", "tenant_id": "test", "role": "buyer"}}),
        "PF_DATABASE_URL": "postgresql://MUST_NOT_OPEN", "PF_ERP_MODE": "erpnext" if target == "erp" else "mock",
        "ERP_BASE_URL": url, "ERP_API_KEY": "LOCAL_SECRET", "ERP_API_SECRET": "LOCAL_SECRET",
        "ERP_COMPANY": "SyntheticCompany", "ERP_EXPECTED_INTEGRATION_USER": "integration@test.invalid",
        "ERP_ALLOW_DRAFT_WRITES": "true", "LLM_BASE_URL": url + "/v1", "LLM_MODEL": "fixture",
        "LLM_API_KEY": "LOCAL_SECRET", "LLM_THINKING_MODE": "enabled"}
    report_path = tmp_path / "report.json"
    try:
        run = subprocess.run([sys.executable, str(root / "scripts/verify_integrations.py"), "--target", target,
            "--allow-network", "--output", str(report_path)], env=env, capture_output=True, text=True, timeout=20)
        assert run.returncode == 0, run.stdout + run.stderr
        result = json.loads(report_path.read_text())
        assert result["network_attempted"] and result["external_business_writes"] == 0
        assert result["live_draft_roundtrip_verified"] is False
        assert "LOCAL_SECRET" not in run.stdout + run.stderr and "fixture-private-continuation" not in report_path.read_text()
        if target == "erp":
            assert len(seen) == 5 and all(method == "GET" for method, _ in seen)
            assert result["checks"]["integration_identity_verified"]
            assert result["checks"]["least_privilege_verified"] is False
        else:
            assert len(seen) == 2 and result["model_calls"] == 2 and result["tool_calls"] == 2
            assert result["usage"]["total_tokens"] == 80
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
