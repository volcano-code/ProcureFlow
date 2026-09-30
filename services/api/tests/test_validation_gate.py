"""Offline fail-closed tests for the PostgreSQL acceptance report, not live DB coverage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("xml, process_code, expected_status, expected_code", [
    (None, 0, "failed", 1),
    ("<broken", 0, "failed", 1),
    ('<testsuites><testsuite tests="34"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="35" skipped="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="35" failures="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="35" errors="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="35"/></testsuites>', 0, "passed", 0),
    (None, 1, "failed", 1),
])
def test_postgres_gate_report_matches_exit_status(tmp_path, monkeypatch, xml, process_code,
                                                expected_status, expected_code):
    spec = importlib.util.spec_from_file_location("postgres_gate_under_test", ROOT / "scripts/verify_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PF_TEST_DATABASE_URL", "postgresql://offline-only/procureflow_test")
    monkeypatch.setenv("PF_ALLOW_DATABASE_TESTS", "1")
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    output = tmp_path / "result"
    def fake_run(*args, **kwargs):
        if xml is not None:
            (output / "postgres.xml").write_text(xml)
        return subprocess.CompletedProcess(args[0], process_code)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.run(output) == expected_code
    report = json.loads((output / "postgres-gate.json").read_text())
    assert report["status"] == expected_status
    assert report["exit_code"] == expected_code
    assert report["live_erp"] is False
    assert report["live_model"] is False
