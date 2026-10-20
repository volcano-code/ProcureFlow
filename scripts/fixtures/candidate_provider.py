"""Loopback-only SCRIPTED provider for the disposable candidate stack gate.

Never a semantic/quality benchmark or a proxy. Has no upstream address, filesystem
credentials or business DB. Control and model endpoints share the Worker's network
namespace, but are not published to the host or API network.
"""
from __future__ import annotations
import argparse
import http.server
import json
import os
import threading

KEY = "synthetic-candidate-stack-key-not-a-provider-secret"
MODEL = "candidate-stack-fixture-v1"
VALUES = dict(supplier_id="Fixture Supplier", sku="STAND-01", quantity="20", uom="EA",
    unit_price="100.00", tax_mode="excluded", tax_rate="0", shipping_cost="0.00",
    discount="0.00", delivery_days=7, currency="CNY")
TEXT = "\n".join(f"{key}: {value}" for key, value in VALUES.items())
MAX_BYTES = 200000


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


class FixtureState:
    def __init__(self):
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.release.set()
        self.calls = 0
        self.waiting = 0
        self.hold = False

    def view(self):
        with self.lock:
            return {"scripted": True, "calls": self.calls, "waiting": self.waiting, "hold": self.hold}

    def control(self, action):
        with self.lock:
            if action == "hold":
                if self.waiting:
                    raise ValueError("ALREADY_WAITING")
                self.hold = True
                self.release.clear()
            elif action == "release":
                self.hold = False
                self.release.set()
            else:
                raise ValueError("INVALID_CONTROL")
        return self.view()


def response_for(body, call_id):
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise ValueError("FIXTURE_MODEL_REQUIRED")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise ValueError("INVALID_MESSAGES")
    if not all(isinstance(x, dict) for x in messages):
        raise ValueError("INVALID_MESSAGES")
    tools = [x for x in messages if x.get("role") == "tool"]
    if not tools:
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"fixture-{call_id}-{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}
            for i, (name, arguments) in enumerate([
                ("get_request", {}), ("get_evidence", {"start": 0, "limit": 24})])]}
        finish = "tool_calls"
    else:
        fragments = None
        for item in tools:
            content = json.loads(item["content"], object_pairs_hook=unique_object)
            data = content.get("data", {})
            if isinstance(data, dict) and isinstance(data.get("fragments"), list):
                fragments = data["fragments"]
        if not fragments:
            raise ValueError("EVIDENCE_REQUIRED")
        # Fixed synthetic answers only, but point each field at the actual bound fragment.
        refs = {}
        for field in VALUES:
            found = [f["id"] for f in fragments
                     if isinstance(f, dict) and f"{field}:" in f.get("text", "")]
            if not found:
                raise ValueError("SYNTHETIC_SOURCE_REQUIRED")
            refs[field] = found[:1]
        result = {"summary": "脚本模拟服务的固定候选，必须由人核对；不是模型效果评测。",
            "values": VALUES, "references": refs, "context_evidence_ids": [], "clarifications": []}
        message = {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}
        finish = "stop"
    return {"choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160}}


def handler_for(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, payload):
            content = json.dumps(payload).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"scripted": True})
            elif self.path == "/state" and self.headers.get("Authorization") == "Bearer " + KEY:
                self.reply(200, state.view())
            else:
                self.reply(404, {"code": "NOT_FOUND"})

        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer " + KEY:
                self.reply(401, {"code": "FIXTURE_AUTH_REQUIRED"}); return
            if self.path not in {"/chat/completions", "/control"}:
                self.reply(404, {"code": "NOT_FOUND"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    self.reply(413, {"code": "BODY_LIMIT"}); return
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length), object_pairs_hook=unique_object)
                if self.path == "/control":
                    if not isinstance(body, dict) or set(body) != {"action"}:
                        raise ValueError("INVALID_CONTROL")
                    self.reply(200, state.control(body["action"])); return
                # Validate before registering a request, without retaining its contents.
                response_for(body, 0)
                with state.lock:
                    if state.calls >= 20:
                        self.reply(429, {"code": "FIXTURE_CALL_LIMIT"}); return
                    state.calls += 1
                    call_id, held = state.calls, state.hold
                    if held: state.waiting += 1
                if held:
                    try:
                        if not state.release.wait(timeout=40):
                            self.reply(503, {"code": "FIXTURE_HOLD_EXPIRED"}); return
                    finally:
                        with state.lock: state.waiting -= 1
                self.reply(200, response_for(body, call_id))
            except (ValueError, TypeError, KeyError, OSError):
                self.reply(400, {"code": "INVALID_FIXTURE_REQUEST"})
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args(argv)
    if not args.fixture or os.environ.get("PF_CANDIDATE_STACK_FIXTURE") != "1":
        print("EXPLICIT_FIXTURE_OPT_IN_REQUIRED")
        return 2
    state = FixtureState()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 19090), handler_for(state))
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        state.release.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
