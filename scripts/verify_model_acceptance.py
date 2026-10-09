"""Bounded, synthetic-only model protocol and durable-advice acceptance.

Default: blocked before reading credentials or constructing clients.
--fixture: offline MockTransport protocol/lifecycle coverage, not vendor acceptance.
--allow-network: one explicitly authorized provider run; charges may apply. Reads
only LLM_BASE_URL, LLM_API_KEY, LLM_MODEL and LLM_THINKING_MODE. Never reads app
DB/auth/ERP settings, uses no production data, and never retries a provider call.
The disposable worker is killed at its total wall deadline. Its only export is
allowlisted validation metrics, never model prose, messages, reasoning or secrets.
Token limits include a provider output cap and a reported-usage stop; reported
usage cannot establish billing cost or preempt an in-flight provider request.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGETS = {"max_model_calls": 4, "max_tool_calls": 8,
                   "max_reported_tokens": 8000, "max_wall_seconds": 35}
SAFE_ERRORS = frozenset({
    "MODEL_NOT_CONFIGURED", "MODEL_ENDPOINT_INVALID", "MODEL_CALL_FAILED", "MODEL_PROTOCOL_INVALID",
    "MODEL_TIMEOUT", "MODEL_CANCELLED", "MODEL_REFUSED", "MODEL_EMPTY_OUTPUT",
    "MODEL_OUTPUT_INCOMPLETE", "MODEL_USAGE_REQUIRED", "MODEL_RESPONSE_LIMIT", "MODEL_SCHEMA_INVALID",
    "MODEL_GROUNDING_REQUIRED", "BUDGET_EXCEEDED", "EVIDENCE_NOT_FOUND", "EVIDENCE_REQUIRED",
    "EVIDENCE_NOT_READ", "TOOL_POLICY_DENIED", "TOOL_ARGUMENTS_INVALID", "TOOL_SCOPE_DENIED",
    "TOOL_CALL_FAILED", "TOOL_OUTPUT_LIMIT", "ADVICE_FAILED", "ADVICE_INTERRUPTED",
})


def _report(kind="not_run", budgets=None):
    return {"schema_version": 1, "evaluation_kind": kind, "status": "blocked", "reason": None,
            "synthetic_input_only": True, "network_attempted": False,
            "external_business_writes": 0, "provider_http_requests": 0,
            "model_calls": 0, "tool_calls": 0, "usage": None, "usage_source": "provider_reported",
            "durable_receipt_verified": False, "replay_prevented": False,
            "business_state_unchanged": False, "evidence_read_verified": False,
            "unknown_shipping_preserved": False, "advisory_only": False,
            "graph_execution_verified": False, "graph_node_count": 0,
            "live_provider_protocol_verified": False, "quality_acceptance_verified": False,
            "semantic_factuality_verified": False, "task_success_rate": None, "cost": None,
            "budgets": dict(budgets or DEFAULT_BUDGETS), "worker_wall_limit_seconds": 0}


def _valid_budgets(budgets):
    limits = {"max_model_calls": (1, 6), "max_tool_calls": (1, 12),
              "max_reported_tokens": (1600, 16000), "max_wall_seconds": (1, 60)}
    return all(type(budgets[key]) is int and low <= budgets[key] <= high
               for key, (low, high) in limits.items())


def _loopback(endpoint):
    host = urlparse(endpoint).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sandbox_environment(directory):
    return {"PYTHONPATH": str(ROOT / "services/api"), "PYTHONIOENCODING": "utf-8",
            "PF_MODE": "demo", "PF_DATA_DIR": directory,
            "PF_DATABASE_URL": f"sqlite:///{Path(directory) / 'acceptance.sqlite3'}",
            "PF_ERP_MODE": "mock", "ERP_ALLOW_DRAFT_WRITES": "false",
            "PF_AUTH_TOKENS": json.dumps({"synthetic-acceptance-buyer": {
                "user_id": "synthetic-buyer", "tenant_id": "synthetic", "role": "buyer"}})}


def _validate_report(result, expected):
    """Fail closed if a worker ever tries to export arbitrary or unbounded data."""
    if not isinstance(result, dict) or set(result) != set(expected):
        raise ValueError("Invalid worker report")
    for key in ("schema_version", "evaluation_kind", "synthetic_input_only", "external_business_writes",
                "quality_acceptance_verified", "semantic_factuality_verified", "task_success_rate", "cost",
                "budgets", "worker_wall_limit_seconds", "usage_source"):
        if result[key] != expected[key]:
            raise ValueError("Invalid fixed metric")
    for key, value in expected.items():
        if type(value) is bool and type(result[key]) is not bool:
            raise ValueError("Invalid boolean metric")
    if result["status"] not in {"passed", "failed"} or result["reason"] not in SAFE_ERRORS | {
            None, "ACCEPTANCE_FAILED", "ACCEPTANCE_CHECK_FAILED", "ACCEPTANCE_WORKER_FAILED"}:
        raise ValueError("Invalid status metric")
    for key, maximum in (("model_calls", expected["budgets"]["max_model_calls"]),
                         ("provider_http_requests", expected["budgets"]["max_model_calls"]),
                         ("tool_calls", expected["budgets"]["max_tool_calls"]),
                         ("graph_node_count", 2 * expected["budgets"]["max_model_calls"])):
        if type(result[key]) is not int or not 0 <= result[key] <= maximum:
            raise ValueError("Invalid counter metric")
    usage = result["usage"]
    if usage is not None and (not isinstance(usage, dict)
            or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(value) is not int or not 0 <= value <= expected["budgets"]["max_reported_tokens"]
                   for value in usage.values())
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise ValueError("Invalid token metric")
    return result


def run_acceptance(allow_network=False, *, fixture=False, **overrides):
    """Run in an isolated worker; no ambient application configuration is read.

    The separate process is important: importing procureflow.app itself creates
    an app, so sanitizing Settings after importing it would already be too late.
    """
    report = _report()
    if fixture and allow_network:
        return {**report, "reason": "CHOOSE_FIXTURE_OR_NETWORK"}, 2
    if not fixture and not allow_network:
        return {**report, "reason": "EXPLICIT_NETWORK_OPT_IN_REQUIRED"}, 2
    if set(overrides) - DEFAULT_BUDGETS.keys():
        return {**report, "reason": "INVALID_BUDGET"}, 2
    budgets = {**DEFAULT_BUDGETS, **overrides}
    if not _valid_budgets(budgets):
        return {**report, "reason": "INVALID_BUDGET"}, 2
    config = {"fixture": fixture, "allow_network": allow_network, "budgets": budgets}
    if fixture:
        kind = "offline_protocol_fixture"
        config.update(base_url="https://fixture.invalid/v1", api_key="synthetic-fixture-token",
                      model="synthetic-fixture", thinking_mode="enabled")
    else:
        # The sole credential read point, strictly after explicit live opt-in.
        config.update(base_url=os.environ.get("LLM_BASE_URL", ""),
                      api_key=os.environ.get("LLM_API_KEY", ""), model=os.environ.get("LLM_MODEL", ""),
                      thinking_mode=os.environ.get("LLM_THINKING_MODE", "default"))
        if not all(config[key] for key in ("base_url", "api_key", "model")):
            return {**report, "reason": "MODEL_CONFIGURATION_REQUIRED"}, 2
        if (config["thinking_mode"] not in {"default", "enabled", "disabled"}
                or len(config["base_url"]) > 2048 or len(config["model"]) > 160
                or len(config["api_key"]) > 8192):
            return {**report, "reason": "INVALID_MODEL_CONFIGURATION"}, 2
        try:
            kind = "loopback_protocol_fixture" if _loopback(config["base_url"]) else "live_provider_protocol_acceptance"
        except (ValueError, TypeError):
            return {**report, "reason": "MODEL_ENDPOINT_INVALID"}, 2
    report = _report(kind, budgets)
    # Includes bounded startup/SQLite/API verification time, not just model time.
    report["worker_wall_limit_seconds"] = budgets["max_wall_seconds"] + 20
    config["evaluation_kind"] = kind
    config["worker_wall_limit_seconds"] = report["worker_wall_limit_seconds"]
    try:
        with tempfile.TemporaryDirectory(prefix="pf-model-acceptance-") as directory:
            # Do not copy or inspect os.environ: even unused app secrets stay out.
            env = _sandbox_environment(directory)
            config["worker_parent_directory"] = directory
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_worker"],
                input=json.dumps(config), text=True, encoding="utf-8", cwd=directory, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=report["worker_wall_limit_seconds"], check=False)
            if len(process.stdout) > 12000 or process.returncode not in (0, 1):
                return {**report, "status": "failed", "reason": "ACCEPTANCE_WORKER_FAILED",
                        "network_attempted": None, "provider_http_requests": None}, 1
            result = json.loads(process.stdout)
            # The worker exports this fixed schema only, never its API response.
            result = _validate_report(result, report)
            if (process.returncode == 0) != (result["status"] == "passed"):
                raise ValueError("Inconsistent worker result")
            return result, process.returncode
    except subprocess.TimeoutExpired:
        return {**report, "status": "failed", "reason": "WORKER_WALL_BUDGET_EXCEEDED",
                "network_attempted": None, "provider_http_requests": None}, 1
    except (OSError, ValueError, TypeError):
        return {**report, "status": "failed", "reason": "ACCEPTANCE_WORKER_FAILED",
                "network_attempted": None, "provider_http_requests": None}, 1


def _fixture_message(body):
    """A protocol fixture, deliberately not a model quality evaluator."""
    messages = body["messages"]
    tool_messages = [message for message in messages if message["role"] == "tool"]
    if not tool_messages:
        message = {"reasoning_content": "PRIVATE_FIXTURE_CONTINUATION", "tool_calls": [
            {"id": "fixture-comparison", "type": "function", "function": {"name": "get_comparison", "arguments": "{}"}},
            {"id": "fixture-policy", "type": "function", "function": {"name": "search_policy", "arguments": "{}"}}]}
    elif len(tool_messages) == 2:
        comparison = json.loads(tool_messages[0]["content"])["data"]
        document_id = comparison["quotes"][0]["document_id"]
        message = {"tool_calls": [{"id": "fixture-evidence", "type": "function", "function": {
            "name": "get_evidence", "arguments": json.dumps({"document_id": document_id})}}]}
    else:
        evidence = json.loads(tool_messages[-1]["content"])["data"]
        message = {"content": json.dumps({"summary": "运费未知，总价不能确定。本说明不构成审批或采购。",
                   "evidence_ids": [evidence["fragments"][0]["id"]]}, ensure_ascii=False)}
    return {"choices": [{"finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 20, "total_tokens": 80}}


def _run_worker(config):
    # This private entry point is also safe if invoked directly: never import
    # application code under an ambient production environment.
    if (type(config.get("fixture")) is not bool or type(config.get("allow_network")) is not bool
            or config["fixture"] == config["allow_network"] or not _valid_budgets(config["budgets"])):
        return {**_report(), "status": "failed", "reason": "INVALID_WORKER_CONFIGURATION"}, 1
    # Nested inside the parent-owned temporary directory so timeout/kill still
    # removes all transient SQLite receipts, source text and provider prose.
    with tempfile.TemporaryDirectory(prefix="pf-acceptance-worker-",
                                     dir=config["worker_parent_directory"]) as directory:
        os.environ.clear()
        os.environ.update(_sandbox_environment(directory))
        return _exercise_api(config)


def _exercise_api(config):
    # Guard before importing code with side effects or constructing any client.
    if config.get("fixture") == config.get("allow_network") or not _valid_budgets(config["budgets"]):
        return {**_report(), "status": "failed", "reason": "INVALID_WORKER_CONFIGURATION"}, 1
    import logging
    logging.disable(logging.CRITICAL)
    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from unittest.mock import patch
    from procureflow.agent import ReadOnlyAgent
    from procureflow.app import create_app
    from procureflow.config import Settings
    from procureflow.db import Base, Database
    from procureflow.erp import MockERP

    report = _report(config["evaluation_kind"], config["budgets"])
    report["worker_wall_limit_seconds"] = config["worker_wall_limit_seconds"]
    requests = 0
    factories = 0
    transport = None
    report["status"] = "failed"
    try:
        class CountedTransport(httpx.BaseTransport):
            def handle_request(self, request):
                nonlocal requests
                if requests >= config["budgets"]["max_model_calls"]:
                    raise RuntimeError("Provider call budget exhausted")
                requests += 1
                report["provider_http_requests"] = report["model_calls"] = requests
                report["network_attempted"] = not config["fixture"]
                return transport.handle_request(request)

            def close(self):
                transport.close()

        def factory():
            nonlocal factories, transport
            factories += 1
            if factories != 1:
                raise RuntimeError("Provider replay forbidden")
            if config["fixture"]:
                transport = httpx.MockTransport(lambda request: httpx.Response(
                    200, json=_fixture_message(json.loads(request.content))))
            else:
                # Explicitly zero connection retries; never replay failed calls.
                transport = httpx.HTTPTransport(retries=0)
            return ReadOnlyAgent(config["base_url"], config["api_key"], config["model"],
                thinking_mode=config["thinking_mode"], require_usage=True,
                required_tools=("get_comparison", "search_policy"), require_evidence_reads=True,
                transport=CountedTransport(), max_connection_retries=0, **config["budgets"])

        settings = Settings()
        database = Database(settings.database_url, create_schema=True)
        erp = MockERP(settings.data_dir / "acceptance-erp.sqlite3")
        app = create_app(settings=settings, database=database, erp=erp)
        headers = {"Authorization": "Bearer synthetic-acceptance-buyer"}

        def business_state():
            with database.transaction() as session:
                return {table.name: sorted(json.dumps(dict(row), sort_keys=True, default=str)
                    for row in session.execute(select(table)).mappings())
                    for table in Base.metadata.sorted_tables
                    if table.name not in {"advice_runs", "audit_events", "alembic_version"}}

        def request(client, method, url, expected=200, **kwargs):
            response = client.request(method, url, headers=headers, **kwargs)
            if response.status_code != expected:
                raise ValueError("Synthetic API protocol failure")
            return response.json()

        with patch.object(ReadOnlyAgent, "from_env", staticmethod(factory)), TestClient(app) as client:
            procurement = request(client, "POST", "/api/v1/requests", expected=201, json={
                "title": "SYNTHETIC model acceptance, no purchase", "sku": "SYNTHETIC-STAND",
                "quantity": "2", "budget": "1000.00", "max_delivery_days": 14})
            request_id = procurement["id"]
            quotation = ("SYNTHETIC quotation; not a real offer\nsupplier_id: SYNTHETIC-A\n"
                "sku: SYNTHETIC-STAND\nquantity: 2\nuom: EA\nunit_price: 100.00\n"
                "tax_mode: included\ntax_rate: 0.13\nshipping_cost: unknown\n"
                "discount: 0.00\ndelivery_days: 7\ncurrency: CNY\n")
            quote = request(client, "POST", f"/api/v1/requests/{request_id}/documents", expected=201,
                            files={"file": ("synthetic-acceptance.txt", quotation.encode())})
            report["unknown_shipping_preserved"] = (quote["values"]["shipping_cost"] is None
                and quote["calculation"]["total"] is None and quote["calculation"]["eligible"] is False)
            before = business_state()
            version = request(client, "GET", f"/api/v1/requests/{request_id}")["version"]
            pending = request(client, "POST", f"/api/v1/requests/{request_id}/advice-runs", expected=201,
                json={"expected_version": version, "idempotency_key": "synthetic-acceptance-0001"})
            if pending["status"] != "PENDING" or requests:
                raise ValueError("Reservation invoked provider")
            run_id = pending["id"]
            result = request(client, "POST", f"/api/v1/advice-runs/{run_id}/process")
            calls_after_process = requests
            # This asks for the existing receipt, never retries an LLM request.
            duplicate = request(client, "POST", f"/api/v1/advice-runs/{run_id}/process")
            report["replay_prevented"] = duplicate == result and requests == calls_after_process and factories == 1
            report["business_state_unchanged"] = business_state() == before and erp.count() == 0
            events = request(client, "GET", f"/api/v1/requests/{request_id}/events")
            report["tool_calls"] = sum(event["type"] == "AGENT_TOOL_COMPLETED" for event in events)
            report["graph_node_count"] = sum(event["type"] == "AGENT_GRAPH_NODE" for event in events)

        # Recreate API and database objects to verify durable persistence/restart.
        reopened = Database(settings.database_url)
        with TestClient(create_app(settings=settings, database=reopened, erp=erp)) as client:
            receipt = request(client, "GET", f"/api/v1/advice-runs/{run_id}")
            report["durable_receipt_verified"] = receipt == result
        if result["status"] != "COMPLETED":
            report["reason"] = result["error_code"] if result["error_code"] in SAFE_ERRORS else "ACCEPTANCE_FAILED"
            return report, 1
        output = result["output"]
        report.update(model_calls=output["model_calls"], tool_calls=output["tool_calls"], usage=output["usage"],
                      evidence_read_verified=output["evidence_read_verified"] is True,
                      advisory_only=output["advisory_only"] is True)
        nodes = [event["node"] for event in output["trace"] if event.get("type") == "graph_node"]
        expected_nodes = [node for _ in range(output["model_calls"] - 1) for node in ("model", "tools")]
        expected_nodes += ["model", "validate"]
        report["graph_execution_verified"] = (output["runtime"] == "langgraph-read-only-v1"
            and nodes == expected_nodes and len(nodes) <= 2 * config["budgets"]["max_model_calls"])
        if config["fixture"]:
            report["graph_execution_verified"] &= nodes == ["model", "tools", "model", "tools", "model", "validate"]
        report["graph_node_count"] = len(nodes)
        checks = ("graph_execution_verified", "durable_receipt_verified", "replay_prevented", "business_state_unchanged",
                  "evidence_read_verified", "unknown_shipping_preserved", "advisory_only")
        if not all(report[key] for key in checks) or not output["usage_complete"]:
            report["reason"] = "ACCEPTANCE_CHECK_FAILED"
            return report, 1
        report["status"] = "passed"
        report["live_provider_protocol_verified"] = config["evaluation_kind"] == "live_provider_protocol_acceptance"
        return report, 0
    except Exception:
        report["reason"] = "ACCEPTANCE_FAILED"
        return report, 1
    finally:
        if transport is not None:
            transport.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--allow-network", action="store_true", help="Authorize one synthetic provider run; charges may apply")
    mode.add_argument("--fixture", action="store_true", help="Offline HTTP protocol fixture, not vendor quality acceptance")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    for name, default in DEFAULT_BUDGETS.items():
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=default)
    args = parser.parse_args(argv)
    if args._worker:
        try:
            config = json.loads(sys.stdin.read(16384))
            result, code = _run_worker(config)
        except Exception:
            result, code = {**_report(), "status": "failed", "reason": "ACCEPTANCE_WORKER_FAILED"}, 1
    else:
        result, code = run_acceptance(args.allow_network, fixture=args.fixture,
            **{name: getattr(args, name) for name in DEFAULT_BUDGETS})
    rendered = json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if args.output is not None and not args._worker:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
