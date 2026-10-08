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


def run(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    report = {"scope": "PostgreSQL migrations, concurrency and workflow; mock ERP only",
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
        start = time.monotonic()
        cmd = [sys.executable, str(ROOT / "scripts/test.py"), "tests/test_workflow.py", "tests/test_postgres.py",
               "tests/test_advice_runs.py", "tests/test_policy_versions.py", "tests/test_policy_adversarial.py",
               "tests/test_table_imports.py", "tests/test_pilot_workflow.py", "tests/test_pilot_auth.py",
               "tests/test_retention.py", "tests/test_maintenance.py", "tests/test_backup_recovery.py",
               "tests/test_advice_grounding.py::test_durable_api_uses_real_adapter_and_persists_source_read_receipt",
               "tests/test_graph_runtime.py::test_durable_api_graph_receipts_are_safe_and_never_replayed",
               "-q", "--junitxml=" + str(output / "postgres.xml")]
        with (output / "postgres.log").open("w") as log:
            result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=log, timeout=360)
        code = result.returncode
        report.update(status="passed" if code == 0 else "failed", exit_code=code,
                      elapsed_seconds=round(time.monotonic() - start, 2))
        if code == 0:
            suites = ET.parse(output / "postgres.xml").getroot().iter("testsuite")
            counts = {k: 0 for k in ("tests", "failures", "errors", "skipped")}
            for suite in suites:
                for key in counts:
                    counts[key] += int(suite.get(key, "0"))
            report["junit"] = counts
            if counts["tests"] < 82 or any(counts[k] for k in ("failures", "errors", "skipped")):
                code = 1
                report.update(status="failed", reason="POSTGRES_TESTS_MISSING_OR_SKIPPED")
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
