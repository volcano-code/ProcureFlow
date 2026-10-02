"""Offline aggregate contract coverage, deterministic regeneration and drift gate."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.routing import APIRoute
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/export_contracts.py"


@pytest.fixture(scope="module")
def exporter():
    spec = importlib.util.spec_from_file_location("contract_export_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(exporter):
    return exporter.generate_contracts()


def test_committed_contracts_match_current_api(generated):
    for name, expected in generated.items():
        assert (ROOT / "packages/contracts" / name).read_bytes() == expected, (
            f"{name} drifted; run python scripts/export_contracts.py and review the diff"
        )


def test_aggregate_covers_every_schema_visible_route_and_preserves_auth(generated):
    from procureflow.app import app

    schema = json.loads(generated["openapi.json"])
    expected = {(route.path_format, method.lower()) for route in app.routes
                if isinstance(route, APIRoute) and route.include_in_schema
                for method in route.methods}
    actual = {(path, method) for path, operations in schema["paths"].items()
              for method in operations if method in {"get", "put", "post", "delete", "patch", "head", "options", "trace"}}
    assert actual == expected
    assert ("/ready", "get") in actual
    for route in (("/api/v1/policy/versions", "get"), ("/api/v1/policy/versions", "post"),
                  ("/api/v1/requests/{request_id}/evaluations", "get"),
                  ("/api/v1/requests/{request_id}/approvals", "get")):
        assert route in actual
    assert ("/api/v1/operations/{operation_id}/verify", "post") in actual
    for path, method in actual:
        if path.startswith("/api/"):
            assert schema["paths"][path][method]["security"] == [{"HTTPBearer": []}]
    assert schema["components"]["securitySchemes"]["HTTPBearer"] == {"type": "http", "scheme": "bearer"}


def test_shared_request_and_quote_schemas_remain_strict(generated):
    from procureflow.contracts import QuoteValues, RequestCreate

    schema = json.loads(generated["openapi.json"])
    request = json.loads(generated["request.schema.json"])
    quotation = json.loads(generated["quotation.schema.json"])
    assert request == RequestCreate.model_json_schema() == schema["components"]["schemas"]["RequestCreate"]
    assert quotation == QuoteValues.model_json_schema()
    # FastAPI removes null defaults from OpenAPI; the standalone schema retains
    # them. Check each against its own generator rather than flattening either.
    assert schema["components"]["schemas"]["QuoteValues"]["properties"].keys() == quotation["properties"].keys()
    for model in schema["components"]["schemas"].values():
        if model.get("title") in {"RequestCreate", "RequestUpdate", "QuoteEdit", "QuoteValues",
                                 "QuoteConfirm", "ApprovalCommand", "ExecuteCommand", "AnalyzeCommand", "PolicyVersionCreate"}:
            assert model["additionalProperties"] is False
    assert request["required"] == ["title", "sku", "quantity", "budget"]
    assert schema["components"]["schemas"]["ExecuteCommand"]["properties"]["snapshot_hash"]["pattern"] == r"^[a-f0-9]{64}$"


@pytest.mark.parametrize("name", ("openapi.json", "quotation.schema.json", "request.schema.json"))
@pytest.mark.parametrize("mutation", ("missing", "malformed", "schema", "format"))
def test_check_fails_on_each_contract_drift_without_rewriting(exporter, generated, monkeypatch, tmp_path, capsys,
                                                             name, mutation):
    monkeypatch.setattr(exporter, "generate_contracts", lambda: generated)
    for filename, content in generated.items():
        (tmp_path / filename).write_bytes(content)
    path = tmp_path / name
    if mutation == "missing":
        path.unlink()
    elif mutation == "malformed":
        path.write_bytes(b"{broken JSON")
    elif mutation == "schema":
        value = json.loads(path.read_bytes())
        value["unexpected_contract_change"] = True
        path.write_text(json.dumps(value))
    else:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert exporter.main(["--check", "--output-dir", str(tmp_path)]) == 1
    assert name in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_check_does_not_create_missing_directory(exporter, generated, monkeypatch, tmp_path):
    monkeypatch.setattr(exporter, "generate_contracts", lambda: generated)
    output = tmp_path / "missing"
    assert exporter.run(output, check=True) == 1
    assert not output.exists()


def test_generation_failure_never_passes_or_changes_contracts(exporter, monkeypatch, tmp_path):
    existing = tmp_path / "openapi.json"
    existing.write_bytes(b"existing contract")

    def fail():
        raise subprocess.CalledProcessError(1, ["schema-export"])

    monkeypatch.setattr(exporter, "generate_contracts", fail)
    assert exporter.main(["--check", "--output-dir", str(tmp_path)]) == 2
    assert exporter.main(["--output-dir", str(tmp_path)]) == 2
    assert existing.read_bytes() == b"existing contract"
    assert list(tmp_path.iterdir()) == [existing]


def test_cli_is_deterministic_and_ignores_ambient_live_configuration(generated, tmp_path):
    protected = tmp_path / "user-data"
    protected.mkdir()
    database = protected / "user.sqlite3"
    database.write_bytes(b"DO NOT OPEN OR MODIFY THIS USER DATABASE")
    env = {**os.environ, "PF_DATA_DIR": str(protected), "PF_DATABASE_URL": f"sqlite:///{database}",
           "PF_MODE": "private", "PF_ERP_MODE": "erpnext", "PF_AUTH_TOKENS": "not-json",
           "PF_WEB_ORIGINS": "not-an-origin", "ERP_ALLOW_DRAFT_WRITES": "true",
           "ERP_BASE_URL": "not-a-valid-endpoint", "ERP_API_KEY": "synthetic-key",
           "ERP_API_SECRET": "synthetic-secret", "ERP_COMPANY": "synthetic-company",
           "LLM_API_KEY": "synthetic-model-key", "PYTHONHASHSEED": "123"}
    output = tmp_path / "export"
    command = [sys.executable, str(SCRIPT), "--output-dir", str(output)]
    first = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    assert {p.name: p.read_bytes() for p in output.iterdir()} == generated
    env["PYTHONHASHSEED"] = "456"
    checked = subprocess.run([*command, "--check"], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stderr
    assert {p.name: p.read_bytes() for p in output.iterdir()} == generated
    assert database.read_bytes() == b"DO NOT OPEN OR MODIFY THIS USER DATABASE"
    assert list(protected.iterdir()) == [database]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export", "user-data"]
