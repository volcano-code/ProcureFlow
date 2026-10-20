"""Fail-closed consistency checker, not a separate execution or model evaluation."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re

STEPS = [
    "verified_native_pg_fixture_scope",
    "browser_upload_reserve_no_model_call",
    "queued_survives_api_web_restart",
    "native_poll_observes_independent_worker",
    "browser_import_is_unconfirmed",
    "terminal_replay_no_second_model_call",
    "cancel_queued_prevents_provider_call",
    "cancel_inflight_discards_late_output",
    "sigkill_pg_interruption_never_replays",
    "role_isolation_and_no_procurement_side_effects",
]
STATUSES = {"normal": "IMPORTED", "queued_cancel": "CANCELLED",
            "inflight_cancel": "CANCELLED", "killed": "INTERRUPTED"}


def require(value, code):
    if not value:
        raise ValueError(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def check(report):
    require(isinstance(report, dict) and report.get("status") == "passed", "RUN_NOT_PASSED")
    require(report.get("schema") == "candidate-stack-fixture-v1", "INVALID_SCHEMA")
    require(report.get("steps") == STEPS, "MISSING_OR_DUPLICATE_STEP")
    for key in ("synthetic_only", "api_without_provider_key", "browser_responses_not_mocked",
                "real_sigkill", "lease_expiry_injected", "no_automatic_replay"):
        require(report.get(key) is True, "SCOPE_NOT_PROVEN")
    for key in ("real_model_used", "real_erp_used", "model_quality_measured"):
        require(report.get(key) is False, "MISLABELED_SCOPE")
    require(report.get("database") == "postgresql", "DATABASE_FALLBACK")
    require(isinstance(report.get("source_commit"), str) and
            re.fullmatch(r"[0-9a-f]{40}", report["source_commit"]), "INVALID_COMMIT")
    require(type(report.get("provider_requests")) is int and report["provider_requests"] == 4,
            "UNEXPECTED_PROVIDER_CALLS")
    rows = report.get("runs")
    require(isinstance(rows, list) and len(rows) == 4 and
            all(isinstance(x, dict) for x in rows), "INVALID_RUNS")
    require({x.get("scenario") for x in rows} == set(STATUSES), "INVALID_SCENARIOS")
    require(all(x.get("status") == STATUSES[x["scenario"]] and isinstance(x.get("id"), str) and
                re.fullmatch(r"can_[0-9a-f]{32}", x["id"]) for x in rows), "INVALID_RECEIPTS")
    require(len({x["id"] for x in rows}) == 4, "DUPLICATE_RUNS")
    counts = report.get("database_counts")
    require(isinstance(counts, dict), "MISSING_DATABASE_AUDIT")
    for key, expected in {"candidate_runs":4, "quotes":1, "confirmed_quotes":0,
                          "approvals":0, "external_operations":0, "candidate_outputs":1}.items():
        require(type(counts.get(key)) is int and counts[key] == expected, "UNEXPECTED_SIDE_EFFECTS")
    return {"status": "passed", "scope": "record consistency only, not an additional execution"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with args.report.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, "OVERSIZED_REPORT")
        result = check(json.loads(raw, object_pairs_hook=unique_object))
    except (ValueError, TypeError, KeyError, OSError):
        print('{"status":"failed","reason":"CANDIDATE_STACK_EVIDENCE_INCOMPLETE_OR_INVALID"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
