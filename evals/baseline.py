"""Run the EXISTING deterministic service against public TaskBundle inputs.

No gold is read here. Field confirmation is a simulated tester action WITHOUT
correction. No approvals, execution worker, ERP endpoint, or LLM are invoked.
Protocol outputs expose current shortcomings rather than patching them with gold.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import time

from .devbench import Observation, QuoteResult, Reference, SafetyCounts, TaskBundle


class NoWriteERP:
    mode = "mock"

    def __init__(self):
        self.write_attempts = 0

    def create_draft(self, *args, **kwargs):
        self.write_attempts += 1
        raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_ERP_WRITES")

    def find(self, *args, **kwargs):
        raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_ERP_LOOKUP")

    def suppliers(self):
        return []


def run_baseline(bundle: TaskBundle, *, attempt: int = 1) -> Observation:
    # Imports are delayed so validation/grading never initialize production state.
    from sqlalchemy import func, select
    from procureflow.config import Settings
    from procureflow.contracts import Principal, QuoteConfirm, RequestCreate
    from procureflow.db import ApprovalRow, Database, OperationRow, QuoteVersionRow
    from procureflow.errors import DomainError
    from procureflow.service import ProcurementService

    class AnalysisOnlyService(ProcurementService):
        approval_attempts = 0

        def approve(self, *args, **kwargs):
            self.approval_attempts += 1
            raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_APPROVAL")

        def enqueue(self, *args, **kwargs):
            raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_DISPATCH")

        def process_operation(self, *args, **kwargs):
            raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_EXECUTION")

        def edit_quote(self, *args, **kwargs):
            raise RuntimeError("OFFLINE_EVALUATION_FORBIDS_GOLD_CORRECTIONS")

    start = time.monotonic()
    quotes, issues, diagnostics, ids = [], [], [], {}
    unsupported, selected, outcome = False, None, "blocked"
    erp = NoWriteERP()
    task = bundle.task
    with tempfile.TemporaryDirectory(prefix="pf-dev-task-") as directory:
        path = Path(directory)
        settings = Settings(data_dir=path, database_url=f"sqlite:///{path / 'evaluation.sqlite3'}",
            mode="demo", auth_tokens={"local-evaluation": {"user_id": "eval-buyer",
                "tenant_id": "eval", "role": "buyer"}}, web_origins=(),
            erp_mode="mock", erp_url="", erp_api_key="", erp_api_secret="",
            erp_company="", erp_allow_draft_writes=False)
        db = Database(settings.database_url, create_schema=True)
        service = AnalysisOnlyService(db, settings, erp)
        principal = Principal(user_id="eval-buyer", tenant_id="eval", role="buyer")
        try:
            request = service.create_request(principal, RequestCreate.model_validate(task.request))
            for source in task.documents:
                if source.kind != "quotation":
                    # Current analyze() uses server-published tenant rules, not uploaded policy documents.
                    # Keep this fixed baseline; do not silently bootstrap rules from gold.
                    continue
                try:
                    q = service.import_quote(principal, request["id"], Path(source.path).name,
                                             bundle.contents[source.id])
                except DomainError as exc:
                    diagnostics.append(f"{source.id}:{exc.code}")
                    unsupported = True
                    continue
                q = service.confirm_quote(principal, q["id"], QuoteConfirm(
                    expected_version=q["version"], acknowledge=True))
                ids[q["id"]] = source.id
                references = {}
                lines = bundle.contents[source.id].decode("utf-8-sig").splitlines()
                for field, evidence in q["evidence"].items():
                    number = evidence.get("line", evidence.get("row"))
                    if type(number) is int and 0 < number <= len(lines):
                        references[field] = [Reference(document_id=source.id, version=source.version,
                            sha256=source.sha256, start_line=number, end_line=number,
                            excerpt=lines[number - 1])]
                quotes.append(QuoteResult(document_id=source.id, values=q["values"],
                    total=q["calculation"]["total"], references=references))
                conflicts = {x.split(":", 1)[1] for x in q["issues"] if x.startswith("CONFLICT:")}
                issues.extend(f"CONFLICT:{source.id}:{field}" for field in conflicts)
                issues.extend(f"MISSING:{source.id}:{field}" for field in q["calculation"]["missing"]
                              if field not in conflicts)
                for violation in q["calculation"]["violations"]:
                    if violation in {"BUDGET_EXCEEDED", "DELIVERY_EXCEEDS_LIMIT", "SKU_MISMATCH",
                                     "UOM_MISMATCH", "QUANTITY_MISMATCH", "UNSUPPORTED_CURRENCY"}:
                        issues.append(f"RULE:{source.id}:{violation}")
                diagnostics.extend(f"{source.id}:{x}" for x in q["issues"])
            analysis = service.analyze(principal, request["id"])
            if analysis["proposal"]:
                selected = ids[analysis["proposal"]["quote_id"]]
                outcome = "ready_for_review"
            elif issues:
                outcome = "needs_clarification" if any(x.startswith(("MISSING:", "CONFLICT:"))
                                                       for x in issues) else "blocked"
            if unsupported:
                # No fallback to a reference answer or generic successful refusal.
                outcome = "unsupported"
                selected = None
            with db.transaction() as session:
                approvals = session.scalar(select(func.count()).select_from(ApprovalRow))
                operations = session.scalar(select(func.count()).select_from(OperationRow))
                edits = session.scalar(select(func.count()).select_from(QuoteVersionRow)
                                       .where(QuoteVersionRow.version > 1))
            safety = SafetyCounts(erp_write_attempts=erp.write_attempts,
                approval_attempts=service.approval_attempts, cross_scope_reads=0, confirmed_field_edits=edits)
            if operations or approvals:
                raise RuntimeError("OFFLINE_EVALUATION_UNEXPECTED_EXECUTION_RESERVATION")
        finally:
            db.engine.dispose()
    return Observation(schema_version=1, task_id=task.id, input_sha256=bundle.input_sha256,
        runner="deterministic-service-v1", attempt=attempt,
        execution_status="unsupported" if unsupported else "executed", outcome=outcome,
        quotes=quotes, selected_document_id=selected, issues=sorted(set(issues)),
        policy_references=[], safety=safety, model_used=False, model_calls=0,
        model_usage=None, cost=None, human_field_confirmation="simulated_without_correction",
        elapsed_seconds=time.monotonic() - start, diagnostics=sorted(set(diagnostics)))
