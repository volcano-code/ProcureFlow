from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from sqlalchemy import select
from .config import Settings
from .contracts import Principal, QuoteValues
from .db import (ApprovalRow, Database, DocumentRow, EventRow, OperationRow, OutboxRow,
                 QuoteRow, QuoteVersionRow, RequestRow, audit, now, uid)
from .domain import POLICY, digest, offer_check
from .erp import ERPPort, ERPRejected, ERPUnknown, remote_matches
from .errors import DomainError
from .parsers import parse_document

FROZEN = {"ERP_PENDING", "RECONCILING", "NEEDS_HUMAN", "ERP_CREATED"}


def require(principal: Principal, role: str):
    if principal.role != role:
        raise DomainError("FORBIDDEN", f"This action requires the {role} role", 403)


def req_dto(row):
    return {"id": row.id, "tenant_id": row.tenant_id, "owner_id": row.owner_id, **row.data,
            "version": row.version, "status": row.status, "proposal": row.proposal, "created_at": row.created_at}


def op_dto(row):
    return {"id": row.id, "request_id": row.request_id, "status": row.status, "attempts": row.attempts,
            "remote_id": row.remote_id, "error": row.error, "snapshot_hash": row.snapshot_hash,
            "created_at": row.created_at}


class ProcurementService:
    def __init__(self, db: Database, settings: Settings, erp: ERPPort):
        self.db, self.settings, self.erp = db, settings, erp
        self.document_dir = settings.data_dir / "documents"
        self.document_dir.mkdir(exist_ok=True)

    def _request(self, session, principal, request_id, lock=False):
        query = select(RequestRow).where(RequestRow.id == request_id, RequestRow.tenant_id == principal.tenant_id)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise DomainError("NOT_FOUND", "Request not found in this workspace", 404)
        return row

    def _quote(self, session, principal, quote_id):
        row = session.scalar(select(QuoteRow).where(QuoteRow.id == quote_id, QuoteRow.tenant_id == principal.tenant_id))
        if row is None:
            raise DomainError("NOT_FOUND", "Quote not found in this workspace", 404)
        version = session.scalar(select(QuoteVersionRow).where(QuoteVersionRow.quote_id == row.id,
                                                            QuoteVersionRow.version == row.current_version))
        return row, version

    def _mutable(self, request):
        if request.status in FROZEN:
            raise DomainError("REQUEST_FROZEN", "Execution has been reserved or completed; editing is locked. Create a new request for a new purchase.")

    def _invalidate(self, session, request):
        had_approval = False
        for approval in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id,
                                                                ApprovalRow.status == "APPROVED")):
            approval.status = "STALE"
            had_approval = True
        request.version += 1
        request.proposal = None
        request.status = "APPROVAL_STALE" if had_approval or request.status == "APPROVAL_STALE" else "NEEDS_CONFIRMATION"

    def _verify_document(self, document):
        path = self.document_dir / document.storage_key
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document.sha256:
            raise DomainError("SOURCE_INTEGRITY_FAILED", "The stored source no longer matches its registered SHA-256")
        return path

    def create_request(self, principal, command):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            row = RequestRow(id=uid("req_"), tenant_id=principal.tenant_id, owner_id=principal.user_id,
                             data=command.model_dump(mode="json"), version=1, status="DRAFT")
            session.add(row)
            session.flush()
            audit(session, principal, row.id, "REQUEST_CREATED", {"version": 1})
            return req_dto(row)

    def list_requests(self, principal):
        with self.db.transaction() as session:
            return [req_dto(row) for row in session.scalars(select(RequestRow).where(
                RequestRow.tenant_id == principal.tenant_id).order_by(RequestRow.created_at.desc()).limit(200))]

    def get_request(self, principal, request_id):
        with self.db.transaction() as session:
            return req_dto(self._request(session, principal, request_id))

    def update_request(self, principal, request_id, command):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            row = self._request(session, principal, request_id, lock=True)
            self._mutable(row)
            if command.expected_version != row.version:
                raise DomainError("VERSION_CONFLICT", "Refresh the request before editing")
            row.data = command.model_dump(mode="json", exclude={"expected_version"})
            self._invalidate(session, row)
            audit(session, principal, row.id, "REQUEST_CHANGED", {"version": row.version, "status": row.status})
            return req_dto(row)

    def import_quote(self, principal, request_id, filename, data):
        require(principal, "buyer")
        if not data or len(data) > self.settings.max_upload_bytes:
            raise DomainError("UPLOAD_LIMIT", "The file must be non-empty and no larger than 2 MiB", 413)
        with self.db.transaction() as session:
            self._mutable(self._request(session, principal, request_id))
        filename = Path(filename.replace("\\", "/")).name[:160]
        document_id = uid("doc_")
        parsed = parse_document(filename, data, document_id)
        path = self.document_dir / (document_id + Path(filename).suffix.lower())
        # Source bytes are not accessible through a static-file route.
        created_file = False
        try:
            with self.db.transaction(write=True) as session:
                request = self._request(session, principal, request_id, lock=True)
                self._mutable(request)
                duplicate = session.scalar(select(DocumentRow).where(DocumentRow.request_id == request_id,
                    DocumentRow.tenant_id == principal.tenant_id, DocumentRow.sha256 == parsed["sha256"]))
                if duplicate:
                    qv = session.scalar(select(QuoteVersionRow).where(QuoteVersionRow.document_id == duplicate.id).order_by(QuoteVersionRow.version.desc()))
                    return self._quote_view(session, session.get(QuoteRow, qv.quote_id), qv, request)
                path.write_bytes(data)
                path.chmod(0o600)
                created_file = True
                document = DocumentRow(id=document_id, tenant_id=principal.tenant_id, request_id=request_id,
                    filename=filename, sha256=parsed["sha256"], storage_key=path.name, fragments=parsed["fragments"])
                quote = QuoteRow(id=uid("quo_"), tenant_id=principal.tenant_id, request_id=request_id, current_version=1)
                session.add_all([document, quote])
                session.flush()
                version = QuoteVersionRow(id=uid("qv_"), quote_id=quote.id, document_id=document_id,
                    version=1, values=parsed["values"], evidence=parsed["evidence"], issues=parsed["issues"])
                session.add(version)
                session.flush()
                self._invalidate(session, request)
                audit(session, principal, request_id, "QUOTE_IMPORTED", {"quote_id": quote.id, "version_id": version.id,
                    "document_id": document_id, "sha256": parsed["sha256"], "parser": parsed["parser"], "issues": parsed["issues"]})
                return self._quote_view(session, quote, version, request)
        except Exception:
            if created_file:
                path.unlink(missing_ok=True)
            raise

    def _quote_view(self, session, quote, version, request):
        document = session.get(DocumentRow, version.document_id)
        return {"id": quote.id, "request_id": quote.request_id, "version_id": version.id, "version": version.version,
                "values": version.values, "evidence": version.evidence, "issues": version.issues,
                "confirmed_by": version.confirmed_by, "filename": document.filename, "document_id": document.id,
                "document_sha256": document.sha256,
                "calculation": offer_check(request.data, version.values, bool(version.confirmed_by))}

    def list_quotes(self, principal, request_id):
        with self.db.transaction() as session:
            request = self._request(session, principal, request_id)
            result = []
            for quote in session.scalars(select(QuoteRow).where(QuoteRow.request_id == request_id).order_by(QuoteRow.id)):
                _, version = self._quote(session, principal, quote.id)
                result.append(self._quote_view(session, quote, version, request))
            return result

    def quote_history(self, principal, quote_id):
        with self.db.transaction() as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id)
            return [self._quote_view(session, quote, version, request) for version in session.scalars(
                select(QuoteVersionRow).where(QuoteVersionRow.quote_id == quote_id).order_by(QuoteVersionRow.version))]

    def edit_quote(self, principal, quote_id, command):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id, lock=True)
            # Refresh after taking the aggregate lock, including on PostgreSQL.
            session.refresh(quote)
            quote, old = self._quote(session, principal, quote_id)
            self._mutable(request)
            if command.expected_version != quote.current_version:
                raise DomainError("VERSION_CONFLICT", "Refresh the quote before editing")
            values = command.values.model_dump(mode="json")
            evidence = dict(old.evidence)
            for key, value in values.items():
                if value != old.values.get(key):
                    evidence[key] = {"kind": "manual", "actor_id": principal.user_id,
                                     "reason": command.reason, "recorded_at": now(), "previous_version_id": old.id}
            quote.current_version += 1
            version = QuoteVersionRow(id=uid("qv_"), quote_id=quote.id, document_id=old.document_id,
                version=quote.current_version, values=values, evidence=evidence, issues=[], confirmed_by=None)
            session.add(version)
            session.flush()
            self._invalidate(session, request)
            audit(session, principal, request.id, "QUOTE_VERSION_CREATED", {"quote_id": quote.id, "version": version.version,
                                                                         "reason": command.reason})
            return self._quote_view(session, quote, version, request)

    def confirm_quote(self, principal, quote_id, command):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id, lock=True)
            session.refresh(quote)
            quote, version = self._quote(session, principal, quote_id)
            self._mutable(request)
            if command.expected_version != quote.current_version:
                raise DomainError("VERSION_CONFLICT", "Only the displayed quote version may be confirmed")
            if not version.confirmed_by:
                self._verify_document(session.get(DocumentRow, version.document_id))
                version.confirmed_by = principal.user_id
                self._invalidate(session, request)
                audit(session, principal, request.id, "QUOTE_CONFIRMED", {"quote_id": quote.id, "version": version.version})
            return self._quote_view(session, quote, version, request)

    def evidence(self, principal, document_id):
        with self.db.transaction() as session:
            document = session.scalar(select(DocumentRow).where(DocumentRow.id == document_id, DocumentRow.tenant_id == principal.tenant_id))
            if not document:
                raise DomainError("NOT_FOUND", "Document not found in this workspace", 404)
            self._verify_document(document)
            return {"id": document.id, "filename": document.filename, "sha256": document.sha256,
                    "trust": "untrusted_source_content", "fragments": document.fragments}

    def _snapshot(self, session, request, quote, version):
        document = session.get(DocumentRow, version.document_id)
        self._verify_document(document)
        checked = offer_check(request.data, version.values, bool(version.confirmed_by))
        if not checked["eligible"]:
            raise DomainError("PROPOSAL_BLOCKED", "Selected quote no longer satisfies all deterministic rules")
        body = {"contract_version": "single-sku-v1", "tenant_id": request.tenant_id, "request_id": request.id,
                "request_version": request.version, "request": request.data, "quote_id": quote.id,
                "quote_version_id": version.id, "quote_version": version.version, "quote_values": version.values,
                "confirmed_by": version.confirmed_by, "evidence_hash": digest(version.evidence),
                "document_sha256": document.sha256, "total": checked["total"],
                "policy_id": POLICY["id"], "policy_version": POLICY["version"], "policy_hash": digest(POLICY),
                "erp_mode": self.erp.mode, "erp_company": self.settings.erp_company,
                "erp_target_fingerprint": digest({"mode": self.erp.mode, "url": self.settings.erp_url}),
                "transaction_date": request.created_at[:10]}
        return {**body, "snapshot_hash": digest(body)}

    def analyze(self, principal, request_id, preferred_quote_id=None):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            self._mutable(request)
            candidates = []
            comparisons = []
            audit(session, principal, request.id, "ANALYSIS_STARTED", {"runtime": "deterministic-baseline", "llm_used": False})
            for quote in session.scalars(select(QuoteRow).where(QuoteRow.request_id == request_id)):
                _, version = self._quote(session, principal, quote.id)
                view = self._quote_view(session, quote, version, request)
                comparisons.append(view)
                if view["calculation"]["eligible"] and (not preferred_quote_id or preferred_quote_id == quote.id):
                    candidates.append((Decimal(view["calculation"]["total"]), quote.id, quote, version))
            audit(session, principal, request.id, "POLICY_CHECK_COMPLETED", {"policy_id": POLICY["id"], "policy_hash": digest(POLICY)})
            if not candidates:
                for approval in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id, ApprovalRow.status == "APPROVED")):
                    approval.status = "STALE"
                request.proposal = None
                request.status = "NEEDS_CONFIRMATION" if any(not v["confirmed_by"] for v in comparisons) else "BLOCKED"
                audit(session, principal, request.id, "ANALYSIS_BLOCKED", {"quote_count": len(comparisons)})
                return {"request": req_dto(request), "quotes": comparisons, "proposal": None,
                        "runtime": "deterministic-baseline", "llm_used": False}
            _, _, quote, version = sorted(candidates, key=lambda x: (x[0], x[1]))[0]
            snapshot = self._snapshot(session, request, quote, version)
            old_hash = (request.proposal or {}).get("snapshot_hash")
            if old_hash != snapshot["snapshot_hash"]:
                for approval in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id, ApprovalRow.status == "APPROVED")):
                    approval.status = "STALE"
                request.status = "READY_FOR_REVIEW"
            elif request.status not in {"APPROVED", "READY_FOR_REVIEW"}:
                request.status = "READY_FOR_REVIEW"
            request.proposal = snapshot
            audit(session, principal, request.id, "PROPOSAL_PREPARED", {"snapshot_hash": snapshot["snapshot_hash"],
                "selected_quote_id": quote.id, "total": snapshot["total"], "runtime": "deterministic-baseline"})
            return {"request": req_dto(request), "quotes": comparisons, "proposal": snapshot,
                    "runtime": "deterministic-baseline", "llm_used": False}

    def _current_snapshot(self, session, principal, request):
        if not request.proposal:
            raise DomainError("APPROVAL_STALE", "Prepare and approve a current proposal first")
        quote, version = self._quote(session, principal, request.proposal["quote_id"])
        return self._snapshot(session, request, quote, version)

    def approve(self, principal, request_id, command):
        require(principal, "approver")
        with self.db.transaction(write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            self._mutable(request)
            if principal.user_id == request.owner_id:
                raise DomainError("SELF_APPROVAL_DENIED", "Requester and approver must be different users", 403)
            current = self._current_snapshot(session, principal, request)
            if command.snapshot_hash != current["snapshot_hash"] or request.proposal["snapshot_hash"] != current["snapshot_hash"]:
                raise DomainError("APPROVAL_STALE", "The displayed snapshot no longer matches the current proposal")
            for previous in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id, ApprovalRow.status == "APPROVED")):
                previous.status = "SUPERSEDED"
            status = "APPROVED" if command.decision == "approve" else "REJECTED"
            approval = ApprovalRow(id=uid("ap_"), request_id=request.id, tenant_id=principal.tenant_id,
                approver_id=principal.user_id, snapshot_hash=command.snapshot_hash, status=status, note=command.note,
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.settings.approval_ttl_seconds)).isoformat())
            session.add(approval)
            session.flush()
            request.status = status
            audit(session, principal, request.id, "APPROVAL_" + status, {"approval_id": approval.id,
                "snapshot_hash": command.snapshot_hash, "expires_at": approval.expires_at})
            return {"id": approval.id, "status": approval.status, "snapshot_hash": approval.snapshot_hash,
                    "expires_at": approval.expires_at, "request": req_dto(request)}

    def _validate_approval(self, session, principal, request, approval=None):
        if approval is None:
            approval = session.scalar(select(ApprovalRow).where(ApprovalRow.request_id == request.id,
                ApprovalRow.status == "APPROVED").order_by(ApprovalRow.created_at.desc()))
        if not approval or approval.status != "APPROVED":
            raise DomainError("APPROVAL_REQUIRED", "A valid approval is required")
        if approval.expires_at <= now():
            raise DomainError("APPROVAL_EXPIRED", "The approval has expired")
        valid_approver = any(identity["user_id"] == approval.approver_id and identity["tenant_id"] == principal.tenant_id
            and identity["role"] == "approver" for identity in self.settings.auth_tokens.values())
        if not valid_approver:
            raise DomainError("APPROVER_REVOKED", "The approving user no longer has approval permission", 403)
        current = self._current_snapshot(session, principal, request)
        if approval.snapshot_hash != current["snapshot_hash"] or request.proposal["snapshot_hash"] != current["snapshot_hash"]:
            raise DomainError("APPROVAL_STALE", "The approved snapshot differs from the current business state")
        return approval, current

    def enqueue(self, principal, request_id, snapshot_hash):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            existing = session.scalar(select(OperationRow).where(OperationRow.request_id == request.id))
            if existing:
                if existing.snapshot_hash != snapshot_hash:
                    raise DomainError("IDEMPOTENCY_PAYLOAD_CONFLICT", "This request already has an operation for a different snapshot")
                return op_dto(existing)
            if request.status != "APPROVED":
                raise DomainError("APPROVAL_REQUIRED", "Approve this unchanged proposal before execution")
            approval, current = self._validate_approval(session, principal, request)
            if snapshot_hash != current["snapshot_hash"]:
                raise DomainError("APPROVAL_STALE", "Execution must use the displayed approved snapshot")
            operation_id = digest({"tenant": principal.tenant_id, "request": request.id, "purpose": "supplier-quotation-draft-v1"})
            operation = OperationRow(id=operation_id, tenant_id=principal.tenant_id, request_id=request.id,
                approval_id=approval.id, snapshot_hash=current["snapshot_hash"], payload=current, status="PENDING", attempts=0)
            session.add(operation)
            session.flush()
            session.add(OutboxRow(id=uid("ob_"), operation_id=operation_id, status="PENDING"))
            request.status = "ERP_PENDING"
            audit(session, principal, request.id, "ERP_OPERATION_RESERVED", {"operation_id": operation_id, "snapshot_hash": snapshot_hash})
            return op_dto(operation)

    def get_operation(self, principal, operation_id):
        with self.db.transaction() as session:
            row = session.scalar(select(OperationRow).where(OperationRow.id == operation_id, OperationRow.tenant_id == principal.tenant_id))
            if not row:
                raise DomainError("NOT_FOUND", "Operation not found in this workspace", 404)
            return op_dto(row)

    def process_operation(self, principal, operation_id):
        require(principal, "buyer")
        with self.db.transaction(write=True) as session:
            operation = session.scalar(select(OperationRow).where(OperationRow.id == operation_id, OperationRow.tenant_id == principal.tenant_id))
            if not operation:
                raise DomainError("NOT_FOUND", "Operation not found in this workspace", 404)
            request = self._request(session, principal, operation.request_id, lock=True)
            session.refresh(operation)
            if operation.status == "COMPLETED":
                return op_dto(operation)
            if operation.status == "IN_FLIGHT" and operation.lease_until and operation.lease_until > now():
                return op_dto(operation)
            fresh = operation.status == "PENDING"
            if fresh:
                try:
                    self._validate_approval(session, principal, request, session.get(ApprovalRow, operation.approval_id))
                except DomainError as error:
                    operation.status, operation.error = "NEEDS_HUMAN", error.code
                    request.status = "NEEDS_HUMAN"
                    session.scalar(select(OutboxRow).where(OutboxRow.operation_id == operation_id)).status = "DONE"
                    audit(session, principal, request.id, "DISPATCH_DENIED", {"operation_id": operation_id, "reason": error.code})
                    return op_dto(operation)
            # Persist intent before any network I/O. Expired IN_FLIGHT is reconciliation-only.
            operation.status = "IN_FLIGHT"
            operation.attempts += 1
            lease = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
            operation.lease_until = lease
            payload = dict(operation.payload)
            audit(session, principal, request.id, "ERP_DISPATCH_STARTED", {"operation_id": operation_id,
                "mode": "create_if_absent" if fresh else "read_only_reconciliation", "erp_mode": self.erp.mode})
        remote, status, error = None, "NEEDS_HUMAN", None
        try:
            remote = self.erp.find(operation_id)
            if remote is None and fresh:
                # A slow lookup must not allow an expired/revoked approval to authorize a new write.
                with self.db.transaction(write=True) as session:
                    request = self._request(session, principal, payload["request_id"], lock=True)
                    operation = session.get(OperationRow, operation_id)
                    if operation.lease_until != lease:
                        return op_dto(operation)
                    self._validate_approval(session, principal, request, session.get(ApprovalRow, operation.approval_id))
                remote = self.erp.create_draft(operation_id, payload)
            if remote is None:
                error = "REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT"
            elif remote_matches(remote, payload):
                status = "COMPLETED"
            else:
                error = "REMOTE_PAYLOAD_MISMATCH"
        except ERPUnknown:
            status, error = "RECONCILING", "ERP_RESULT_UNKNOWN"
        except ERPRejected as rejection:
            error = str(rejection)[:80]
        except DomainError as denial:
            error = denial.code
        except Exception:
            # Unexpected adapter failures are also uncertain, never implicit success.
            status, error = "RECONCILING", "ERP_ADAPTER_FAILURE"
        with self.db.transaction(write=True) as session:
            request = self._request(session, principal, payload["request_id"], lock=True)
            operation = session.get(OperationRow, operation_id)
            if operation.lease_until != lease:
                return op_dto(operation)
            operation.status, operation.error, operation.lease_until = status, error, None
            operation.remote_id = remote.get("name") if status == "COMPLETED" and remote else None
            request.status = {"COMPLETED": "ERP_CREATED", "RECONCILING": "RECONCILING"}.get(status, "NEEDS_HUMAN")
            session.scalar(select(OutboxRow).where(OutboxRow.operation_id == operation_id)).status = "PENDING" if status == "RECONCILING" else "DONE"
            audit(session, principal, request.id, "ERP_" + status, {"operation_id": operation_id,
                "remote_id": operation.remote_id, "error": error, "simulated": self.erp.mode == "mock"})
            return op_dto(operation)

    def events(self, principal, request_id, after=0):
        with self.db.transaction() as session:
            self._request(session, principal, request_id)
            return [{"id": row.id, "type": row.type, "actor_id": row.actor_id, "payload": row.payload, "created_at": row.created_at}
                    for row in session.scalars(select(EventRow).where(EventRow.request_id == request_id,
                        EventRow.tenant_id == principal.tenant_id, EventRow.id > after).order_by(EventRow.id).limit(1000))]
