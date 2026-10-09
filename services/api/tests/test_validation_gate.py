"""Offline fail-closed tests for the PostgreSQL acceptance report, not live DB coverage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[3]

PROTECTION_TEST = "test_postgres_protection_preserves_evidence_and_recovery_posture"
MODES = ("plain", "signed", "encrypted", "signed-encrypted")
SNAPSHOT_TEST = "test_snapshot_change_during_provider_response_stops_tool_and_future_calls"
RECEIPT_TEST = "test_failure_receipt_is_durable_sanitized_and_never_replayed"
MODEL_CASES = (
    *(f"{SNAPSHOT_TEST}[{change}]" for change in ("request", "policy", "source")),
    *(f"{RECEIPT_TEST}[fault{index}-{code}]" for index, code in enumerate((
        "MODEL_TIMEOUT", "MODEL_REFUSED", "MODEL_EMPTY_OUTPUT", "MODEL_OUTPUT_INCOMPLETE"))),
)


def _protected_report(modes=MODES, classname="tests.test_backup_protection", *,
                      model_cases=MODEL_CASES, model_classname="tests.test_model_failures",
                      child_case=None, child_status=None, tests=89):
    cases = "".join(f'<testcase classname="{classname}" name="{PROTECTION_TEST}[{mode}]"/>' for mode in modes)
    cases += "".join(f'<testcase classname="{model_classname}" name="{name}"/>' for name in model_cases)
    if child_status:
        cases = cases.replace(f'name="{child_case}"/>', f'name="{child_case}"><{child_status}/></testcase>')
    return f'<testsuites><testsuite tests="{tests}">' + cases + '</testsuite></testsuites>'


@pytest.mark.parametrize("xml, process_code, expected_status, expected_code", [
    (None, 0, "failed", 1),
    ("<broken", 0, "failed", 1),
    (_protected_report(tests=88), 0, "failed", 1),
    ('<testsuites><testsuite tests="89" skipped="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="89" failures="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="89" errors="1"/></testsuites>', 0, "failed", 1),
    ('<testsuites><testsuite tests="89"/></testsuites>', 0, "failed", 1),
    (_protected_report(MODES[:-1]), 0, "failed", 1),
    (_protected_report(classname="tests.test_backup_recovery"), 0, "failed", 1),
    (_protected_report(model_cases=()), 0, "failed", 1),
    (_protected_report(model_classname="tests.test_graph_runtime"), 0, "failed", 1),
    (_protected_report(model_cases=(SNAPSHOT_TEST, RECEIPT_TEST)), 0, "failed", 1),
    (_protected_report(model_cases=MODEL_CASES[:-1] + (MODEL_CASES[0],)), 0, "failed", 1),
    (_protected_report(), 0, "passed", 0),
    (None, 1, "failed", 1),
])
def test_postgres_gate_report_matches_exit_status(tmp_path, monkeypatch, xml, process_code,
                                                expected_status, expected_code):
    report = _run_gate(tmp_path, monkeypatch, xml, process_code, expected_code)
    assert report["status"] == expected_status


def _run_gate(tmp_path, monkeypatch, xml, process_code=0, expected_code=1, *, stale_report=None):
    spec = importlib.util.spec_from_file_location("postgres_gate_under_test", ROOT / "scripts/verify_postgres.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PF_TEST_DATABASE_URL", "postgresql://offline-only/procureflow_test")
    monkeypatch.setenv("PF_ALLOW_DATABASE_TESTS", "1")
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    output = tmp_path / "result"
    if stale_report is not None:
        output.mkdir()
        (output / "postgres.xml").write_text(stale_report)
    def fake_run(*args, **kwargs):
        assert "tests/test_advice_runs.py" in args[0]
        assert "tests/test_policy_versions.py" in args[0]
        assert "tests/test_policy_adversarial.py" in args[0]
        assert "tests/test_retention.py" in args[0]
        assert "tests/test_maintenance.py" in args[0]
        assert "tests/test_backup_recovery.py" in args[0]
        assert "tests/test_backup_protection.py::" + PROTECTION_TEST in args[0]
        assert "tests/test_backup_protection.py" not in args[0]
        assert "tests/test_pilot_workflow.py" in args[0]
        assert "tests/test_advice_grounding.py::test_durable_api_uses_real_adapter_and_persists_source_read_receipt" in args[0]
        assert "tests/test_graph_runtime.py::test_durable_api_graph_receipts_are_safe_and_never_replayed" in args[0]
        assert {selection for selection in args[0] if selection.startswith("tests/test_model_failures.py")} == {
            "tests/test_model_failures.py::" + SNAPSHOT_TEST,
            "tests/test_model_failures.py::" + RECEIPT_TEST,
        }
        assert kwargs["env"]["PF_TEST_BACKEND"] == "postgresql"
        assert kwargs["env"]["PF_REQUIRE_POSTGRES"] == "1"
        assert not (output / "postgres.xml").exists()
        if xml is not None:
            (output / "postgres.xml").write_text(xml)
        return subprocess.CompletedProcess(args[0], process_code)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.run(output) == expected_code
    report = json.loads((output / "postgres-gate.json").read_text())
    assert report["status"] == ("passed" if expected_code == 0 else "failed")
    assert report["exit_code"] == expected_code
    assert report["live_erp"] is False
    assert report["live_model"] is False
    assert report["sqlite_fallback"] is False

    if expected_code == 0:
        assert report["postgres_protection_modes"] == list(MODES)
        assert report["postgres_model_failure_cases"] == [
            "tests/test_model_failures.py::" + case for case in sorted(MODEL_CASES)]
    else:
        assert "postgres_protection_modes" not in report
        assert "postgres_model_failure_cases" not in report
    return report


@pytest.mark.parametrize("missing", MODEL_CASES)
def test_postgres_gate_requires_every_durable_model_failure_case(tmp_path, monkeypatch, missing):
    xml = _protected_report(model_cases=tuple(case for case in MODEL_CASES if case != missing))
    report = _run_gate(tmp_path, monkeypatch, xml)
    assert report["reason"] == "POSTGRES_MODEL_FAILURE_COVERAGE_MISSING"


@pytest.mark.parametrize("child_status", ("failure", "error", "skipped"))
@pytest.mark.parametrize("child_case", (*MODEL_CASES, *(f"{PROTECTION_TEST}[{mode}]" for mode in MODES)))
def test_postgres_gate_rejects_failed_required_case_despite_clean_suite_totals(
        tmp_path, monkeypatch, child_case, child_status):
    xml = _protected_report(child_case=child_case, child_status=child_status)
    report = _run_gate(tmp_path, monkeypatch, xml)
    assert report["reason"] == ("POSTGRES_PROTECTED_RECOVERY_MISSING" if child_case.startswith(PROTECTION_TEST)
                                else "POSTGRES_MODEL_FAILURE_COVERAGE_MISSING")


def test_postgres_gate_cannot_reuse_stale_passing_report(tmp_path, monkeypatch):
    _run_gate(tmp_path, monkeypatch, None, stale_report=_protected_report())
