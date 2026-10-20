"""Run/grade synthetic Dev tasks; task failures are NOT infrastructure failures.

Commands:
  validate         verify inputs, frozen gold, hashes and citation locations
  predict-baseline run legacy domain service, no gold access, no live adapters
  grade            evaluate exactly one runner and one attempt, no best-of retries
  self-check       verify gold-shaped records pass the grader; NOT a model test

All report files are exclusive-create. --require-all-success makes any task
failure a failing exit status. Missing observations always fail. No network,
ERP writes or real-model calls are implemented by this command.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "services/api"))
from pydantic import ValidationError
from evals.devbench import (BenchmarkError, Observation, SafetyCounts, fingerprint,
    grade_all, load_gold, load_inputs, read_json, write_json)


def implementation_fingerprint() -> str:
    # No gold files here. Bind observations to the evaluated source, not a filename.
    paths = sorted((ROOT / "services/api/procureflow").glob("*.py"))
    paths += [ROOT / name for name in ("evals/baseline.py", "evals/devbench.py", "scripts/evaluate_dev.py")]
    return fingerprint({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def self_check(bundles, gold):
    answers = {g.task_id: g for g in gold.answers}
    records = []
    for bundle in bundles:
        answer = answers[bundle.task.id]
        records.append(Observation(schema_version=1, task_id=bundle.task.id,
            input_sha256=bundle.input_sha256, runner="gold-shaped-grader-self-check", attempt=1,
            execution_status="executed", outcome=answer.outcome, quotes=answer.quotes,
            selected_document_id=answer.selected_document_id, issues=answer.issues,
            policy_references=answer.policy_references,
            safety=SafetyCounts(erp_write_attempts=0, approval_attempts=0,
                                cross_scope_reads=0, confirmed_field_edits=0),
            model_used=False, model_calls=0, model_usage=None, cost=None,
            human_field_confirmation="none", elapsed_seconds=0.0))
    result = grade_all(bundles, gold, records)
    result["purpose"] = "GRADER_SELF_CHECK_ONLY_NOT_APPLICATION_OR_MODEL_PERFORMANCE"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "predict-baseline", "grade", "self-check"))
    parser.add_argument("--suite", type=Path, default=ROOT / "evals/dev12")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--attempt", type=int, choices=range(1, 101), default=1)
    parser.add_argument("--require-all-success", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise BenchmarkError("OUTPUT_EXISTS_PRESERVE_PREVIOUS_TRIAL")
        manifest, bundles = load_inputs(args.suite)
        provenance = {"input_manifest_sha256": fingerprint(manifest.model_dump(mode="json")),
                      "implementation_sha256": implementation_fingerprint(),
                      "network_enabled": False, "real_erp_used": False}
        exit_code = 0
        if args.command == "predict-baseline":
            # PUBLIC inputs only. Do not read gold or infer a response from category.
            from evals.baseline import run_baseline
            observations = [run_baseline(bundle, attempt=args.attempt).model_dump(mode="json") for bundle in bundles]
            report = {"schema_version": 1, "purpose": "OBSERVED_DETERMINISTIC_BASELINE",
                      **provenance, "observations": observations}
        else:
            gold = load_gold(args.suite, bundles)
            provenance["gold_sha256"] = hashlib.sha256((args.suite / "gold/answers.json").read_bytes()).hexdigest()
            if args.command == "validate":
                report = {"status": "passed", "purpose": "DATASET_INTEGRITY_ONLY",
                          "tasks": len(bundles), "source_documents": sum(len(b.contents) for b in bundles),
                          "split": "Dev", "reference_review": gold.review_status, **provenance}
            elif args.command == "self-check":
                report = {**self_check(bundles, gold), **provenance}
                exit_code = 0 if report["all_tasks_passed"] else 1
            else:
                if args.observations is None:
                    raise BenchmarkError("OBSERVATIONS_REQUIRED")
                raw = read_json(args.observations)
                if (not isinstance(raw, dict) or raw.get("schema_version") != 1
                        or raw.get("input_manifest_sha256") != provenance["input_manifest_sha256"]
                        or raw.get("implementation_sha256") != provenance["implementation_sha256"]):
                    raise BenchmarkError("OBSERVATION_MANIFEST_MISMATCH")
                observations = [Observation.model_validate(x) for x in raw["observations"]]
                report = {**grade_all(bundles, gold, observations), **provenance,
                          "observations_sha256": hashlib.sha256(args.observations.read_bytes()).hexdigest()}
                if not report["execution_complete"] or (args.require_all_success and not report["all_tasks_passed"]):
                    exit_code = 1
        write_json(args.output, report)
        print(json.dumps({"command": args.command, "exit_code": exit_code,
                          "task_counts": report.get("counts"), "all_tasks_passed": report.get("all_tasks_passed"),
                          "notice": "Exit 0 without --require-all-success means the measurement ran, NOT that all tasks passed."}))
        return exit_code
    except (BenchmarkError, ValidationError, OSError, ValueError, TypeError, KeyError, RuntimeError):
        # Do not log malformed source values, paths, credentials or raw exception text.
        print(json.dumps({"status": "failed", "reason": "EVALUATION_INPUT_OR_EXECUTION_INVALID"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
