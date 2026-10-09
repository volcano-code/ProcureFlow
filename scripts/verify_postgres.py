"""Strict opt-in PostgreSQL gate. Never falls back to SQLite for PG tests."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PROTECTION_MODES = ("plain", "signed", "encrypted", "signed-encrypted")
PROTECTION_TEST = "test_postgres_protection_preserves_evidence_and_recovery_posture"
# These cases use system -> pg_database when PF_TEST_BACKEND=postgresql.
# The other test_model_failures cases only exercise a mock model transport.
MODEL_FAILURE_TESTS = {
    "test_snapshot_change_during_provider_response_stops_tool_and_future_calls": (
        "request", "policy", "source"),
    "test_failure_receipt_is_durable_sanitized_and_never_replayed": (
        "fault0-MODEL_TIMEOUT", "fault1-MODEL_REFUSED", "fault2-MODEL_EMPTY_OUTPUT",
        "fault3-MODEL_OUTPUT_INCOMPLETE"),
}
MINIMUM_TESTS = 82 + sum(len(cases) for cases in MODEL_FAILURE_TESTS.values())


def passed_cases(results: ET.Element, module: str) -> set[str]:
    """Do not treat a named but skipped/failed/error case as acceptance evidence."""
    return {case.get("name", "") for case in results.iter("testcase")
            if case.get("classname", "").split(".")[-1] == module
            and not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))}


def run(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    report = {"scope": "PostgreSQL migrations, concurrency, workflow, durable model-failure receipts and synthetic protected recovery; mock ERP/model transports only",
              "status": "blocked", "live_erp": False, "live_model": False, "sqlite_fallback": False}
    code = 2
    try:
        if (not os.getenv("PF_TEST_DATABASE_URL") or os.getenv("PF_ALLOW_DATABASE_TESTS") != "1"):
            report["reason"] = "POSTGRES_TEST_CONFIGURATION_REQUIRED"
            return code
        if importlib.util.find_spec("psycopg") is None:
            report["reason"] = "PSYCOPG_NOT_INSTALLED"
            return code
        env = {**os.environ, "PF_TEST_BACKEND": "postgresql", "PF_REQUIRE_POSTGRES": "1"}
        junit_path = output / "postgres.xml"
        # A successful command that emits no report must not reuse an older pass.
        junit_path.unlink(missing_ok=True)
        start = time.monotonic()
        cmd = [sys.executable, str(ROOT / "scripts/test.py"), "tests/test_workflow.py", "tests/test_postgres.py",
               "tests/test_advice_runs.py", "tests/test_policy_versions.py", "tests/test_policy_adversarial.py",
               "tests/test_table_imports.py", "tests/test_pilot_workflow.py", "tests/test_pilot_auth.py",
               "tests/test_retention.py", "tests/test_maintenance.py", "tests/test_backup_recovery.py",
               "tests/test_backup_protection.py::" + PROTECTION_TEST,
               "tests/test_recovery_diagnostics.py", "tests/test_multi_item_procurement.py",
               "tests/test_multi_item_imports.py", "tests/test_multi_item_erp.py",
               "tests/test_advice_grounding.py::test_durable_api_uses_real_adapter_and_persists_source_read_receipt",
               "tests/test_graph_runtime.py::test_durable_api_graph_receipts_are_safe_and_never_replayed",
               *("tests/test_model_failures.py::" + name for name in MODEL_FAILURE_TESTS),
               "-q", "--junitxml=" + str(junit_path)]
        with (output / "postgres.log").open("w") as log:
            result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=log, timeout=360)
        code = result.returncode
        report.update(status="passed" if code == 0 else "failed", exit_code=code,
                      elapsed_seconds=round(time.monotonic() - start, 2))
        if code == 0:
            results = ET.parse(junit_path).getroot()
            suites = results.iter("testsuite")
            counts = {k: 0 for k in ("tests", "failures", "errors", "skipped")}
            for suite in suites:
                for key in counts:
                    counts[key] += int(suite.get(key, "0"))
            report["junit"] = counts
            if counts["tests"] < MINIMUM_TESTS or any(counts[k] for k in ("failures", "errors", "skipped")):
                code = 1
                report.update(status="failed", reason="POSTGRES_TESTS_MISSING_OR_SKIPPED")
            else:
                # The broader gate also includes explicit SQLite-only regressions.
                # Only these pg_database-backed cases establish protected PG recovery.
                expected = {f"{PROTECTION_TEST}[{mode}]" for mode in PROTECTION_MODES}
                actual = passed_cases(results, "test_backup_protection")
                model_expected = {f"{name}[{case}]" for name, cases in MODEL_FAILURE_TESTS.items()
                                  for case in cases}
                model_actual = passed_cases(results, "test_model_failures")
                if not expected.issubset(actual):
                    code = 1
                    report.update(status="failed", reason="POSTGRES_PROTECTED_RECOVERY_MISSING")
                elif not model_expected.issubset(model_actual):
                    code = 1
                    report.update(status="failed", reason="POSTGRES_MODEL_FAILURE_COVERAGE_MISSING")
                else:
                    report["postgres_protection_modes"] = list(PROTECTION_MODES)
                    report["postgres_model_failure_cases"] = [
                        "tests/test_model_failures.py::" + case for case in sorted(model_expected)]
        return code
    except (ET.ParseError, ValueError):
        code = 1
        report.update(status="failed", reason="POSTGRES_REPORT_INVALID")
        return code
    except (subprocess.TimeoutExpired, OSError):
        code = 1
        report.update(status="failed", reason="POSTGRES_RUN_FAILED")
        return code
    finally:
        report["exit_code"] = code
        (output / "postgres-gate.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports/postgres-gate")
    args = parser.parse_args()
    raise SystemExit(run(args.output.resolve()))
