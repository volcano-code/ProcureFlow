"""Durable, version-bound advice receipts around the existing read-only adapter.

No model messages, private reasoning, credentials or raw errors are persisted.
A claimed run is never replayed, even after a crash or an uncertain HTTP result.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from .contracts import AdviceRunCommand
from .db import AdviceRunRow, DocumentRow, QuoteRow, audit, now, uid
from .domain import digest
from .policies import effective_policy
from .errors import DomainError
from .service import require

LEASE_SECONDS = 90
# Only fixed application codes can become a durable failure receipt.
SAFE_ERRORS = frozenset({
    "MODEL_NOT_CONFIGURED", "MODEL_ENDPOINT_INVALID", "MODEL_CALL_FAILED", "MODEL_PROTOCOL_INVALID",
    "MODEL_OUTPUT_INCOMPLETE", "MODEL_USAGE_REQUIRED", "MODEL_RESPONSE_LIMIT", "MODEL_SCHEMA_INVALID",
    "MODEL_GROUNDING_REQUIRED", "BUDGET_EXCEEDED", "EVIDENCE_NOT_FOUND", "EVIDENCE_REQUIRED",
    "EVIDENCE_NOT_READ", "TOOL_POLICY_DENIED", "TOOL_ARGUMENTS_INVALID", "TOOL_SCOPE_DENIED",
    "TOOL_CALL_FAILED", "TOOL_OUTPUT_LIMIT", "SOURCE_INTEGRITY_FAILED", "ADVICE_CONTEXT_LIMIT",
})


def expired(row):
    return row.status == "RUNNING" and (not row.lease_until or row.lease_until <= now())


class AdviceService:
    def __init__(self, procurement):
        self.procurement = procurement
        self.db = procurement.db

    def _run(self, session, principal, run_id, lock=False):
        query = select(AdviceRunRow).where(AdviceRunRow.id == run_id,
                                           AdviceRunRow.tenant_id == principal.tenant_id)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise DomainError("NOT_FOUND", "Advice run not found in this workspace", 404)
        return row

    def _capture(self, session, principal, request):
        quotes, documents = [], {}
        policy = effective_policy(session, principal.tenant_id)
        rows = list(session.scalars(select(QuoteRow).where(QuoteRow.request_id == request.id,
                     QuoteRow.tenant_id == principal.tenant_id).order_by(QuoteRow.id).limit(21)))
        if len(rows) > 20:
            raise DomainError("ADVICE_CONTEXT_LIMIT", "Advice supports at most 20 quotes per request", 422)
        for quote in rows:
            quote, version = self.procurement._quote(session, principal, quote.id, refresh=True)
            document = session.get(DocumentRow, version.document_id)
            try:
                self.procurement._verify_document(document)
            except OSError as error:
                raise DomainError("SOURCE_INTEGRITY_FAILED", "Advice source is unavailable") from error
            quotes.append(self.procurement._quote_view(session, quote, version, request, policy))
            documents[document.id] = {"id": document.id, "filename": document.filename,
                "sha256": document.sha256, "trust": "untrusted_source_content", "fragments": document.fragments}
        result = {"request": {"id": request.id, "version": request.version, **request.data},
                  "quotes": quotes, "documents": documents, "policy": policy,
                  "quote_collection_hash": digest(quotes)}
        if len(json.dumps(result, ensure_ascii=False)) > 200000:
            raise DomainError("ADVICE_CONTEXT_LIMIT", "Advice source context exceeds its limit", 422)
        return deepcopy(result)

    def _freshness(self, session, principal, request, row):
        if row.input_snapshot is None:
            return False, "LEGACY_INPUT_UNBOUND"
        if digest(row.input_snapshot) != row.input_hash:
            return False, "ADVICE_INPUT_CORRUPTED"
        if request.version != row.request_version:
            return False, "ADVICE_INPUT_CHANGED"
        try:
            snapshot = self._capture(session, principal, request)
            same_policy = snapshot["policy"]["policy_hash"] == effective_policy(session, principal.tenant_id)["policy_hash"]
            return (True, None) if same_policy and digest(snapshot) == row.input_hash else (False, "ADVICE_INPUT_CHANGED")
        except DomainError as error:
            return False, error.code if error.code in SAFE_ERRORS else "ADVICE_INPUT_CHANGED"

    def _dto(self, row, current, stale_reason):
        interrupted = expired(row)
        if row.input_snapshot is None:
            current, stale_reason = False, "LEGACY_INPUT_UNBOUND"
        elif digest(row.input_snapshot) != row.input_hash:
            current, stale_reason = False, "ADVICE_INPUT_CORRUPTED"
        return {"id": row.id, "request_id": row.request_id, "request_version": row.request_version,
                "input_hash": row.input_hash, "input_snapshot": row.input_snapshot,
                "policy_version": (row.input_snapshot or {}).get("policy", {}).get("version"),
                "policy_hash": (row.input_snapshot or {}).get("policy", {}).get("policy_hash"),
                "quote_collection_hash": (row.input_snapshot or {}).get("quote_collection_hash"), "status": "INTERRUPTED" if interrupted else row.status,
                "created_at": row.created_at, "started_at": row.started_at, "completed_at": row.completed_at,
                "error_code": "ADVICE_INTERRUPTED" if interrupted else row.error_code,
                "output": row.output, "current": current, "stale_reason": stale_reason}

    def reserve(self, principal, request_id, command: AdviceRunCommand):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            request = self.procurement._request(session, principal, request_id, lock=True)
            row = session.scalar(select(AdviceRunRow).where(AdviceRunRow.tenant_id == principal.tenant_id,
                AdviceRunRow.request_id == request_id, AdviceRunRow.idempotency_key == command.idempotency_key))
            if row is not None:
                if command.expected_version != row.request_version:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "This key already belongs to another request version")
                return self._dto(row, *self._freshness(session, principal, request, row))
            if request.version != command.expected_version:
                raise DomainError("VERSION_CONFLICT", "Refresh the request before starting advice")
            snapshot = self._capture(session, principal, request)
            row = AdviceRunRow(id=uid("adv_"), tenant_id=principal.tenant_id, request_id=request_id,
                actor_id=principal.user_id, idempotency_key=command.idempotency_key,
                request_version=request.version, input_hash=digest(snapshot), input_snapshot=snapshot, status="PENDING")
            session.add(row)
            session.flush()
            audit(session, principal, request_id, "ADVICE_RESERVED", {"run_id": row.id,
                "request_version": row.request_version, "input_hash": row.input_hash, "advisory_only": True})
            return self._dto(row, True, None)

    def get(self, principal, run_id):
        with self.db.transaction(write=True) as session:
            row = self._run(session, principal, run_id)
            request = self.procurement._request(session, principal, row.request_id, lock=True)
            return self._dto(row, *self._freshness(session, principal, request, row))

    def list(self, principal, request_id):
        with self.db.transaction(write=True) as session:
            request = self.procurement._request(session, principal, request_id, lock=True)
            rows = list(session.scalars(select(AdviceRunRow).where(AdviceRunRow.tenant_id == principal.tenant_id,
                AdviceRunRow.request_id == request_id).order_by(AdviceRunRow.created_at.desc(), AdviceRunRow.id).limit(20)))
            # Capture once for the complete response, not once per historical run.
            try:
                captured = self._capture(session, principal, request)
                input_hash, reason = digest(captured), None
                if captured["policy"]["policy_hash"] != effective_policy(session, principal.tenant_id)["policy_hash"]:
                    input_hash, reason = None, "ADVICE_INPUT_CHANGED"
            except DomainError as error:
                input_hash, reason = None, error.code if error.code in SAFE_ERRORS else "ADVICE_INPUT_CHANGED"
            return [self._dto(row, row.input_hash == input_hash,
                    None if row.input_hash == input_hash else reason or "ADVICE_INPUT_CHANGED") for row in rows]

    def process(self, principal, run_id, factory):
        require(principal, "buyer")
        # Lock order is tenant, request, then run, matching policy publication and edits.
        with self.db.transaction() as session:
            request_id = self._run(session, principal, run_id).request_id
        with self.db.transaction(write=True) as session:
            request = self.procurement._request(session, principal, request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            if expired(row):
                self._finish(session, principal, row, "INTERRUPTED", "ADVICE_INTERRUPTED")
            if row.status != "PENDING":
                return self._dto(row, *self._freshness(session, principal, request, row))
            current, reason = self._freshness(session, principal, request, row)
            if not current:
                self._finish(session, principal, row, "STALE", reason)
                return self._dto(row, False, reason)
            snapshot = deepcopy(row.input_snapshot)
            row.status, row.started_at = "RUNNING", now()
            row.lease_until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
            audit(session, principal, request_id, "ADVICE_STARTED", {"run_id": run_id, "advisory_only": True})

        documents = snapshot["documents"]
        evidence_ids = {fragment["id"] for document in documents.values() for fragment in document["fragments"]}
        def invoke(name, arguments):
            if name == "get_comparison":
                return {"request": snapshot["request"], "quotes": snapshot["quotes"]}
            if name == "search_policy":
                return snapshot["policy"]
            if name == "get_evidence" and arguments.get("document_id") in documents:
                return documents[arguments["document_id"]]
            raise DomainError("TOOL_SCOPE_DENIED", "Document is outside this advice run", 403)

        def observe(event):
            # Adapter emits only bounded metadata; no messages/reasoning/tool text.
            with self.db.transaction(write=True) as session:
                audit(session, principal, request_id, "AGENT_" + event["type"].upper(), {**event, "run_id": run_id})

        agent, output, error_code = None, None, None
        try:
            agent = factory()
            agent.require_evidence_reads = True
            output = agent.run(invoke, evidence_ids, observer=observe)
        except DomainError as error:
            error_code = error.code if error.code in SAFE_ERRORS else "ADVICE_FAILED"
        except Exception:
            # Unknown provider/programming errors must not leak secrets or leave a reusable claim.
            error_code = "ADVICE_FAILED"
        finally:
            if agent is not None:
                try:
                    agent.client.close()
                except Exception:
                    error_code = "ADVICE_FAILED"

        with self.db.transaction(write=True) as session:
            request = self.procurement._request(session, principal, request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            current, reason = self._freshness(session, principal, request, row)
            if expired(row):
                self._finish(session, principal, row, "INTERRUPTED", "ADVICE_INTERRUPTED")
            elif row.status == "RUNNING":
                if not current:
                    row.output = output
                    self._finish(session, principal, row, "STALE", reason)
                elif error_code:
                    self._finish(session, principal, row, "FAILED", error_code)
                else:
                    row.output = output
                    self._finish(session, principal, row, "COMPLETED", None)
            return self._dto(row, current, reason)

    def _finish(self, session, principal, row, status, error_code):
        row.status, row.error_code, row.completed_at, row.lease_until = status, error_code, now(), None
        audit(session, principal, row.request_id, "ADVICE_" + status, {"run_id": row.id,
            "error_code": error_code, "input_hash": row.input_hash, "advisory_only": True})
