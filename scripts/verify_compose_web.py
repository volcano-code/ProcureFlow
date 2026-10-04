"""Strict native-container browser gate against an explicitly allowed local demo.

Does not start/stop containers or access a real ERP/model. The test suite creates
synthetic procurement records; run only on disposable data. No static fallback.
"""
from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import httpx

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "test_native_table_csv_maps_source_unknown_freight_and_never_auto_confirms",
    "test_native_table_xlsx_sheet_header_row_formula_and_cancel_navigation",
    "test_native_table_cancelled_upload_ignores_late_result",
    "test_native_table_revision_conflict_and_lost_confirmation_require_readback",
    "test_native_policy_conflict_requires_refresh_and_deliberate_reentry",
    "test_native_policy_future_version_does_not_activate_early",
    "test_native_policy_role_history_stale_evaluation_and_strictest_limits",
    "test_native_success_evidence_and_persisted_request", "test_native_edit_request_invalidates_approval",
    "test_native_rejection_blocks_execution", "test_native_identity_change_clears_evidence_and_prior_tenant",
    "test_proxy_keeps_auth_and_scope_boundaries", "test_browser_uses_only_same_origin_business_api",
    "test_audit_sse_reaches_browser_through_native_proxy",
    "test_native_advice_real_backend_unconfigured_history",
    "test_native_advice_unconfigured_never_calls_model",
    "test_native_advice_mock_provider_persisted_history_and_no_replay",
    "test_native_advice_mock_failure_interrupted_stale_and_recovery",
    "test_native_advice_mock_late_reservation_cannot_process_after_request_switch",
    "test_native_advice_mock_late_process_cannot_leak_across_identity",
    "test_native_advice_mock_read_failure_is_recoverable_without_writes",
    "test_native_advice_mock_lost_process_response_reads_receipt_without_replay",
    "test_native_advice_mock_citation_opens_verified_real_source",
    "test_native_advice_mock_business_audit_event_refreshes_freshness",
}


def source_identity() -> dict:
    """Identify the checked-out source separately from the GitHub run's head SHA.

    Pull-request jobs may test a synthetic merge commit. Keep both commit and
    tree so the evidence consumer can establish exact source equivalence.
    """
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=ROOT,
                                capture_output=True, text=True, check=True, timeout=5)
        commit, tree = result.stdout.strip().splitlines()
        if all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (commit, tree)):
            return {"source_commit": commit, "source_tree": tree}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"source_commit": None, "source_tree": None}


def report_passed(path: Path) -> bool:
    try:
        root = ET.parse(path).getroot()
        cases = list(root.iter("testcase"))
        return (len(cases) == len(EXPECTED) and {c.get("name") for c in cases} == EXPECTED
                and sum(int(s.get("tests", "0")) for s in root.iter("testsuite")) == len(cases)
                and not any(list(root.iter(tag)) for tag in ("failure", "error", "skipped"))
                and all(int(s.get(k, "0")) == 0 for s in root.iter("testsuite") for k in ("failures", "errors", "skipped")))
    except (OSError, ET.ParseError, ValueError, TypeError):
        return False


def run(base_url: str, output: Path, allow_mutations: bool) -> int:
    report = {"scope": "native Next container + same-origin proxy + PostgreSQL/mock API",
              "status": "blocked", "browser_verified": False, "fallback_to_static_demo": False,
              "live_erp": False, "live_model": False, **source_identity()}
    output.mkdir(parents=True, exist_ok=True)
    code = 2
    try:
        url = urlparse(base_url)
        if (not allow_mutations or url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            report["reason"] = "EXPLICIT_LOOPBACK_DEMO_TARGET_REQUIRED"
            return code
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False, follow_redirects=False) as client:
            health = client.get("/backend/health")
            ready = client.get("/backend/ready")
            html = client.get("/")
            if (health.status_code != 200 or health.json().get("mode") != "demo" or health.json().get("erp") != "mock"
                    or ready.status_code != 200 or ready.json().get("database") != "postgresql"
                    or html.status_code != 200 or "/_next/" not in html.text):
                report["reason"] = "NATIVE_DEMO_POSTGRES_TARGET_REQUIRED"
                return code
        env = {**os.environ, "PF_NEXT_TEST_URL": base_url.rstrip("/"), "PF_ALLOW_TEST_MUTATIONS": "1",
               "PF_SCREENSHOT_DIR": str(output.resolve()), "PF_REQUIRE_BROWSER": "1",
               "PF_BROWSER_TRACE_DIR": str(output.resolve() / "traces")}
        for key in list(env):
            if key.startswith(("ERP_", "LLM_")) or key in {"PF_AUTH_TOKENS", "PF_DATABASE_URL"}:
                env.pop(key, None)
        xml = output.resolve() / "native-container.xml"
        xml.unlink(missing_ok=True)  # An old green XML must never certify this run.
        command = [sys.executable, "-m", "pytest", "apps/web/e2e/workbench_e2e.py",
                   "apps/web/e2e/proxy_e2e.py", "-q", "--junitxml=" + str(xml)]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        (output / "browser.log").write_text(result.stdout + result.stderr)
        passed = result.returncode == 0 and report_passed(xml)
        code = 0 if passed else 1
        report.update(status="passed" if passed else "failed", browser_verified=passed,
                      expected_browser_cases=len(EXPECTED), pytest_exit_code=result.returncode)
    except (httpx.HTTPError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        report.update(status="failed", reason="NATIVE_CONTAINER_GATE_FAILED")
        code = 1
    finally:
        report["exit_code"] = code
        (output / "native-container-gate.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--allow-test-mutations", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.base_url, args.output.resolve(), args.allow_test_mutations))
