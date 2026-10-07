from __future__ import annotations

import hashlib
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from sqlalchemy import select
from .config import Settings
from .auth import IdentityService
from .contracts import Principal, QuoteValues
from .db import (ApprovalRow, Database, DocumentRow, EventRow, OperationRow, OutboxRow,
                 QuoteRow, QuoteVersionRow, RequestRow, EvaluationRow, TenantPolicyRow, PolicyVersionRow, PilotMembershipRow, PilotSessionRow, audit, now, uid)
from .domain import digest, offer_check
from .policies import bootstrap, effective_policy, lock_tenant, policy_versions, publish
from .erp import COST_MAPPING_VERSION, ERPPort, ERPRejected, ERPUnknown, remote_matches
from .errors import DomainError
from .tabular import isolated_parse_document as parse_document
from .table_imports import TableImportServiceMixin

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


class ProcurementService(TableImportServiceMixin):
    def __init__(self, db: Database, settings: Settings, erp: ERPPort):
        self.db, self.settings, self.erp = db, settings, erp
        self.identities = IdentityService(db, settings)
        self.document_dir = settings.data_dir / "documents"
        self.document_dir.mkdir(exist_ok=True)
        with self.db.transaction(write=True) as session:
            for tenant in sorted({p["tenant_id"] for p in settings.auth_tokens.values()}):
                bootstrap(session, tenant)

    @contextmanager
    def transaction(self, principal, write=False):
        """Revalidate durable authority inside the business transaction, never a cache.

        Pilot writes lock tenant → identity → session before aggregate locks. Revocation
        uses the same order, so either it commits first and this action is denied, or
        this already-authorized action finishes before revocation takes effect.
        """
        with self.db.transaction(write=write) as session:
            self.identities.validate_principal(session, principal, lock=write)
            yield session
            # Time can pass even while revocation is locked out. Roll back local
            # mutations that crossed expiry; external write receipts use their own
            # finalization transaction and are never rolled back by session expiry.
            self.identities.validate_principal(session, principal, lock=write)

    def _request(self, session, principal, request_id, lock=False):
        if lock:
            lock_tenant(session, principal.tenant_id)
        query = select(RequestRow).where(RequestRow.id == request_id, RequestRow.tenant_id == principal.tenant_id)
        row = session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
        if row is None:
            raise DomainError("NOT_FOUND", "Request not found in this workspace", 404)
        return row

    def policy(self, principal):
        with self.transaction(principal) as session:
            current = effective_policy(session, principal.tenant_id)
            return {**current, "latest_version": session.get(TenantPolicyRow, principal.tenant_id).latest_version,
                    "status": "effective"}

    def policy_history(self, principal):
        with self.transaction(principal) as session:
            return policy_versions(session, principal.tenant_id)

    def publish_policy(self, principal, command):
        require(principal, "approver")
        with self.transaction(principal, write=True) as session:
            row = publish(session, principal, command)
            active = effective_policy(session, principal.tenant_id)
            if active["version"] == row.version:
                # Future revisions are invalidated lazily at every current-state gate.
                for request in session.scalars(select(RequestRow).where(RequestRow.tenant_id == principal.tenant_id)
                                                .order_by(RequestRow.id).with_for_update()):
                    if request.proposal and request.proposal.get("policy_hash") != active["policy_hash"]:
                        for approval in session.scalars(select(ApprovalRow).where(
                                ApprovalRow.request_id == request.id, ApprovalRow.status == "APPROVED")):
                            approval.status = "STALE"
                        if request.status not in FROZEN:
                            request.status = "APPROVAL_STALE"
                        audit(session, principal, request.id, "POLICY_CHANGED", {
                            "policy_version": row.version, "policy_hash": row.policy_hash,
                            "previous_policy_version": request.proposal.get("policy_version")})
            return next(item for item in policy_versions(session, principal.tenant_id) if item["id"] == row.id)

    def _request_dto(self, session, principal, row):
        result = req_dto(row)
        current, reason = False, None
        if row.proposal:
            try:
                current = self._current_snapshot(session, principal, row)["snapshot_hash"] == row.proposal["snapshot_hash"]
                reason = None if current else "PROPOSAL_INPUT_CHANGED"
            except DomainError as error:
                reason = error.code
            if not current and row.status in {"APPROVED", "READY_FOR_REVIEW", "REJECTED"}:
                result["status"] = "APPROVAL_STALE"
        result.update(proposal_current=current, proposal_stale_reason=reason)
        return result

    def _quote(self, session, principal, quote_id, refresh=False):
        row = session.scalar(select(QuoteRow).where(QuoteRow.id == quote_id, QuoteRow.tenant_id == principal.tenant_id)
                             .execution_options(populate_existing=refresh))
        if row is None:
            raise DomainError("NOT_FOUND", "Quote not found in this workspace", 404)
        version = session.scalar(select(QuoteVersionRow).where(QuoteVersionRow.quote_id == row.id,
                                                            QuoteVersionRow.version == row.current_version)
                                 .execution_options(populate_existing=refresh))
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
        with self.transaction(principal, write=True) as session:
            lock_tenant(session, principal.tenant_id)
            row = RequestRow(id=uid("req_"), tenant_id=principal.tenant_id, owner_id=principal.user_id,
                             data=command.model_dump(mode="json"), version=1, status="DRAFT")
            session.add(row)
            session.flush()
            audit(session, principal, row.id, "REQUEST_CREATED", {"version": 1})
            return self._request_dto(session, principal, row)

    def list_requests(self, principal):
        with self.transaction(principal) as session:
            return [self._request_dto(session, principal, row) for row in session.scalars(select(RequestRow).where(
                RequestRow.tenant_id == principal.tenant_id).order_by(RequestRow.created_at.desc()).limit(200))]

    def get_request(self, principal, request_id):
        with self.transaction(principal) as session:
            return self._request_dto(session, principal, self._request(session, principal, request_id))

    def update_request(self, principal, request_id, command):
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            row = self._request(session, principal, request_id, lock=True)
            self._mutable(row)
            if command.expected_version != row.version:
                raise DomainError("VERSION_CONFLICT", "Refresh the request before editing")
            row.data = command.model_dump(mode="json", exclude={"expected_version"})
            self._invalidate(session, row)
            audit(session, principal, row.id, "REQUEST_CHANGED", {"version": row.version, "status": row.status})
            return self._request_dto(session, principal, row)

    def import_quote(self, principal, request_id, filename, data):
        require(principal, "buyer")
        if not data or len(data) > self.settings.max_upload_bytes:
            raise DomainError("UPLOAD_LIMIT", "The file must be non-empty and no larger than 2 MiB", 413)
        with self.transaction(principal) as session:
            self._mutable(self._request(session, principal, request_id))
        filename = Path(filename.replace("\\", "/")).name[:160]
        document_id = uid("doc_")
        parsed = parse_document(filename, data, document_id)
        path = self.document_dir / (document_id + Path(filename).suffix.lower())
        # Source bytes are not accessible through a static-file route.
        created_file = False
        try:
            with self.transaction(principal, write=True) as session:
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

    def _quote_view(self, session, quote, version, request, policy=None):
        document = session.get(DocumentRow, version.document_id)
        return {"id": quote.id, "request_id": quote.request_id, "version_id": version.id, "version": version.version,
                "values": version.values, "evidence": version.evidence, "issues": version.issues,
                "confirmed_by": version.confirmed_by, "filename": document.filename, "document_id": document.id,
                "document_sha256": document.sha256,
                "calculation": offer_check(request.data, version.values, bool(version.confirmed_by),
                                           policy or effective_policy(session, request.tenant_id))}

    def list_quotes(self, principal, request_id):
        with self.transaction(principal) as session:
            request = self._request(session, principal, request_id)
            result = []
            for quote in session.scalars(select(QuoteRow).where(QuoteRow.request_id == request_id).order_by(QuoteRow.id)):
                _, version = self._quote(session, principal, quote.id)
                result.append(self._quote_view(session, quote, version, request))
            return result

    def quote_history(self, principal, quote_id):
        with self.transaction(principal) as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id)
            return [self._quote_view(session, quote, version, request) for version in session.scalars(
                select(QuoteVersionRow).where(QuoteVersionRow.quote_id == quote_id).order_by(QuoteVersionRow.version))]

    def edit_quote(self, principal, quote_id, command):
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id, lock=True)
            # Refresh after taking the aggregate lock, including on PostgreSQL.
            session.refresh(quote)
            quote, old = self._quote(session, principal, quote_id, refresh=True)
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
        with self.transaction(principal, write=True) as session:
            quote, _ = self._quote(session, principal, quote_id)
            request = self._request(session, principal, quote.request_id, lock=True)
            session.refresh(quote)
            quote, version = self._quote(session, principal, quote_id, refresh=True)
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
        with self.transaction(principal) as session:
            document = session.scalar(select(DocumentRow).where(DocumentRow.id == document_id, DocumentRow.tenant_id == principal.tenant_id))
            if not document:
                raise DomainError("NOT_FOUND", "Document not found in this workspace", 404)
            self._verify_document(document)
            return {"id": document.id, "filename": document.filename, "sha256": document.sha256,
                    "trust": "untrusted_source_content", "fragments": document.fragments}

    def _binding(self, session, request, policy=None):
        """Bind the complete sorted collection, including ineligible/unconfirmed offers."""
        policy = policy or effective_policy(session, request.tenant_id)
        quotes = []
        for quote in session.scalars(select(QuoteRow).where(QuoteRow.request_id == request.id,
                QuoteRow.tenant_id == request.tenant_id).order_by(QuoteRow.id)):
            version = session.scalar(select(QuoteVersionRow).where(QuoteVersionRow.quote_id == quote.id,
                                                        QuoteVersionRow.version == quote.current_version))
            document = session.get(DocumentRow, version.document_id)
            self._verify_document(document)
            quotes.append(self._quote_view(session, quote, version, request, policy))
        return {"request_id": request.id, "request_version": request.version, "request": deepcopy(request.data),
                "policy": policy, "quote_collection": quotes, "quote_collection_hash": digest(quotes)}

    def _valid_quote_count(self, binding):
        # Multiple files or revisions from one supplier are not independent competition.
        return len({q["values"]["supplier_id"] for q in binding["quote_collection"] if q["calculation"]["eligible"]})

    def _snapshot(self, session, request, quote, version, binding=None):
        binding = binding or self._binding(session, request)
        policy = binding["policy"]
        document = session.get(DocumentRow, version.document_id)
        checked = offer_check(request.data, version.values, bool(version.confirmed_by), policy)
        if not checked["eligible"] or self._valid_quote_count(binding) < policy["minimum_valid_quotes"]:
            raise DomainError("PROPOSAL_BLOCKED", "Selected quote or valid supplier count no longer satisfies policy")
        body = {"contract_version": "single-sku-v2", "tenant_id": request.tenant_id, "request_id": request.id,
                "request_version": request.version, "request": deepcopy(request.data), "quote_id": quote.id,
                "quote_version_id": version.id, "quote_version": version.version, "quote_values": deepcopy(version.values),
                "confirmed_by": version.confirmed_by, "evidence_hash": digest(version.evidence),
                "document_sha256": document.sha256, "total": checked["total"],
                "policy_id": policy["id"], "policy_version": policy["version"], "policy_hash": policy["policy_hash"],
                "policy": policy, "quote_collection": binding["quote_collection"],
                "quote_collection_hash": binding["quote_collection_hash"], "input_hash": digest(binding),
                "valid_quote_count": self._valid_quote_count(binding),
                "erp_mode": self.erp.mode, "erp_company": self.settings.erp_company,
                "erp_cost_mapping_version": COST_MAPPING_VERSION,
                "erp_cost_accounts": {"tax": self.settings.erp_tax_account, "freight": self.settings.erp_freight_account},
                "erp_target_fingerprint": digest({"mode": self.erp.mode, "url": self.settings.erp_url}),
                "transaction_date": request.created_at[:10]}
        return {**body, "snapshot_hash": digest(body)}

    def _evaluation_dto(self, row, binding=None, reason=None):
        current = binding is not None and digest(binding) == row.input_hash
        return {"id": row.id, "request_id": row.request_id, "request_version": row.request_version,
                "created_at": row.created_at, "created_by": row.actor_id, "policy_version": row.input_snapshot["policy"]["version"],
                "policy_hash": row.input_snapshot["policy"]["policy_hash"],
                "quote_collection_hash": row.input_snapshot["quote_collection_hash"], "input_hash": row.input_hash,
                "input_snapshot": row.input_snapshot, "result": row.result,
                "current": current, "stale_reason": None if current else reason or "EVALUATION_INPUT_CHANGED"}

    def evaluations(self, principal, request_id, offset=0, limit=100):
        with self.transaction(principal, write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            try:
                binding, reason = self._binding(session, request), None
            except DomainError as error:
                binding, reason = None, error.code
            return [self._evaluation_dto(row, binding, reason) for row in session.scalars(select(EvaluationRow).where(
                EvaluationRow.request_id == request_id, EvaluationRow.tenant_id == principal.tenant_id)
                .order_by(EvaluationRow.created_at.desc(), EvaluationRow.id).offset(offset).limit(limit))]

    def analyze(self, principal, request_id, preferred_quote_id=None):
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            self._mutable(request)
            binding = self._binding(session, request)
            policy, comparisons = binding["policy"], binding["quote_collection"]
            valid_count = self._valid_quote_count(binding)
            candidates = [view for view in comparisons if view["calculation"]["eligible"]
                          and (not preferred_quote_id or preferred_quote_id == view["id"])]
            violations = []
            if valid_count < policy["minimum_valid_quotes"]:
                violations.append("INSUFFICIENT_VALID_QUOTES")
            if not candidates:
                violations.append("NO_ELIGIBLE_QUOTE")
            audit(session, principal, request.id, "ANALYSIS_STARTED", {"runtime": "deterministic-baseline", "llm_used": False})
            audit(session, principal, request.id, "POLICY_CHECK_COMPLETED", {"policy_id": policy["id"],
                "policy_version": policy["version"], "policy_hash": policy["policy_hash"],
                "valid_quote_count": valid_count, "minimum_valid_quotes": policy["minimum_valid_quotes"], "violations": violations})
            snapshot = None
            if violations:
                for approval in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id,
                                                                          ApprovalRow.status == "APPROVED")):
                    approval.status = "STALE"
                request.proposal = None
                request.status = "NEEDS_CONFIRMATION" if any(not v["confirmed_by"] for v in comparisons) else "BLOCKED"
                audit(session, principal, request.id, "ANALYSIS_BLOCKED", {"quote_count": len(comparisons), "violations": violations})
            else:
                selected = min(candidates, key=lambda view: (Decimal(view["calculation"]["total"]), view["id"]))
                quote, version = self._quote(session, principal, selected["id"])
                snapshot = self._snapshot(session, request, quote, version, binding)
                if (request.proposal or {}).get("snapshot_hash") != snapshot["snapshot_hash"]:
                    for approval in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request.id,
                                                                              ApprovalRow.status == "APPROVED")):
                        approval.status = "STALE"
                    request.status = "READY_FOR_REVIEW"
                elif request.status not in {"APPROVED", "READY_FOR_REVIEW"}:
                    request.status = "READY_FOR_REVIEW"
                request.proposal = snapshot
                audit(session, principal, request.id, "PROPOSAL_PREPARED", {"snapshot_hash": snapshot["snapshot_hash"],
                    "selected_quote_id": quote.id, "total": snapshot["total"], "runtime": "deterministic-baseline"})
            result = {"quotes": comparisons, "proposal": snapshot, "violations": violations,
                      "valid_quote_count": valid_count, "minimum_valid_quotes": policy["minimum_valid_quotes"]}
            receipt = EvaluationRow(id=uid("eval_"), tenant_id=principal.tenant_id, request_id=request_id,
                actor_id=principal.user_id, request_version=request.version, input_hash=digest(binding),
                input_snapshot=binding, result=deepcopy(result))
            session.add(receipt)
            session.flush()
            return {"request": self._request_dto(session, principal, request), **result, "policy": policy,
                    "evaluation": self._evaluation_dto(receipt, binding),
                    "runtime": "deterministic-baseline", "llm_used": False}

    def _current_snapshot(self, session, principal, request):
        if not request.proposal:
            raise DomainError("APPROVAL_STALE", "Prepare and approve a current proposal first")
        binding = self._binding(session, request)
        if request.proposal.get("input_hash") != digest(binding):
            raise DomainError("APPROVAL_STALE", "The complete quote collection, request or effective policy has changed")
        quote, version = self._quote(session, principal, request.proposal["quote_id"])
        snapshot = self._snapshot(session, request, quote, version, binding)
        self._assert_policy_current(session, request, snapshot["policy_hash"])
        return snapshot

    def _assert_policy_current(self, session, request, policy_hash):
        if effective_policy(session, request.tenant_id)["policy_hash"] != policy_hash:
            raise DomainError("APPROVAL_STALE", "A new policy became effective during validation")

    def approve(self, principal, request_id, command):
        require(principal, "approver")
        with self.transaction(principal, write=True) as session:
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
                approver_id=principal.user_id, approver_auth_version=principal.auth_version, snapshot_hash=command.snapshot_hash, snapshot=deepcopy(current), status=status, note=command.note,
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.settings.approval_ttl_seconds)).isoformat())
            session.add(approval)
            session.flush()
            request.status = status
            audit(session, principal, request.id, "APPROVAL_" + status, {"approval_id": approval.id,
                "snapshot_hash": command.snapshot_hash, "expires_at": approval.expires_at})
            return {"id": approval.id, "status": approval.status, "snapshot_hash": approval.snapshot_hash,
                    "expires_at": approval.expires_at, "snapshot": approval.snapshot,
                    "request": self._request_dto(session, principal, request)}

    def approvals(self, principal, request_id, offset=0, limit=100):
        with self.transaction(principal, write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            try:
                current, reason = self._current_snapshot(session, principal, request), None
            except DomainError as error:
                current, reason = None, error.code
            results = []
            for row in session.scalars(select(ApprovalRow).where(ApprovalRow.request_id == request_id,
                    ApprovalRow.tenant_id == principal.tenant_id).order_by(ApprovalRow.created_at.desc(), ApprovalRow.id)
                    .offset(offset).limit(limit)):
                matches = bool(row.snapshot and current and row.snapshot_hash == current["snapshot_hash"])
                results.append({"id": row.id, "request_id": request_id, "approver_id": row.approver_id,
                    "snapshot_hash": row.snapshot_hash, "snapshot": row.snapshot,
                    "status": "STALE" if row.status == "APPROVED" and not matches else row.status,
                    "stored_status": row.status, "note": row.note, "created_at": row.created_at,
                    "expires_at": row.expires_at, "current": matches,
                    "stale_reason": None if matches else reason or "APPROVAL_STALE"})
            return results

    def _validate_approval(self, session, principal, request, approval=None):
        if approval is None:
            approval = session.scalar(select(ApprovalRow).where(ApprovalRow.request_id == request.id,
                ApprovalRow.status == "APPROVED").order_by(ApprovalRow.created_at.desc()))
        if approval and approval.status == "STALE":
            raise DomainError("APPROVAL_STALE", "This approval is bound to obsolete inputs")
        if not approval or approval.status != "APPROVED":
            raise DomainError("APPROVAL_REQUIRED", "A valid approval is required")
        if approval.expires_at <= now():
            raise DomainError("APPROVAL_EXPIRED", "The approval has expired")
        if not self.identities.approver_valid(session, principal.tenant_id, approval.approver_id,
                auth_version=approval.approver_auth_version, lock=True):
            raise DomainError("APPROVER_REVOKED", "The approving user no longer has approval permission", 403)
        current = self._current_snapshot(session, principal, request)
        if approval.snapshot_hash != current["snapshot_hash"] or request.proposal["snapshot_hash"] != current["snapshot_hash"]:
            raise DomainError("APPROVAL_STALE", "The approved snapshot differs from the current business state")
        # Snapshot/source validation may be slow; expiry and revocation must be
        # checked again at authorization, not only at validation entry.
        if approval.expires_at <= now():
            raise DomainError("APPROVAL_EXPIRED", "The approval expired during validation")
        if not self.identities.approver_valid(session, principal.tenant_id, approval.approver_id,
                auth_version=approval.approver_auth_version, lock=True):
            raise DomainError("APPROVER_REVOKED", "The approving user no longer has approval permission", 403)
        return approval, current

    def enqueue(self, principal, request_id, snapshot_hash):
        require(principal, "buyer")
        with self.transaction(principal, write=True) as session:
            request = self._request(session, principal, request_id, lock=True)
            existing = session.scalar(select(OperationRow).where(OperationRow.request_id == request.id))
            if existing:
                if existing.snapshot_hash != snapshot_hash:
                    raise DomainError("IDEMPOTENCY_PAYLOAD_CONFLICT", "This request already has an operation for a different snapshot")
                return op_dto(existing)
            if request.status == "APPROVAL_STALE":
                raise DomainError("APPROVAL_STALE", "Re-analyze and approve the effective policy first")
            if request.status != "APPROVED":
                raise DomainError("APPROVAL_REQUIRED", "Approve this unchanged proposal before execution")
            approval, current = self._validate_approval(session, principal, request)
            if snapshot_hash != current["snapshot_hash"]:
                raise DomainError("APPROVAL_STALE", "Execution must use the displayed approved snapshot")
            operation_id = digest({"tenant": principal.tenant_id, "request": request.id, "purpose": "supplier-quotation-draft-v1"})
            operation = OperationRow(id=operation_id, tenant_id=principal.tenant_id, request_id=request.id,
                approval_id=approval.id, initiator_id=principal.user_id, initiator_auth_version=principal.auth_version,
                snapshot_hash=current["snapshot_hash"], payload=current, status="PENDING", attempts=0)
            session.add(operation)
            session.flush()
            session.add(OutboxRow(id=uid("ob_"), operation_id=operation_id, status="PENDING"))
            request.status = "ERP_PENDING"
            audit(session, principal, request.id, "ERP_OPERATION_RESERVED", {"operation_id": operation_id, "snapshot_hash": snapshot_hash})
            return op_dto(operation)

    def get_operation(self, principal, operation_id):
        with self.transaction(principal) as session:
            row = session.scalar(select(OperationRow).where(OperationRow.id == operation_id, OperationRow.tenant_id == principal.tenant_id))
            if not row:
                raise DomainError("NOT_FOUND", "Operation not found in this workspace", 404)
            return op_dto(row)

    def _execution_target_matches(self, payload):
        """Recovery must not consult another ERP or trust a modified stored snapshot."""
        return (payload.get("erp_mode") == self.erp.mode
                and payload.get("erp_company") == self.settings.erp_company
                and payload.get("erp_cost_mapping_version") == COST_MAPPING_VERSION
                and payload.get("erp_cost_accounts") == {"tax": self.settings.erp_tax_account, "freight": self.settings.erp_freight_account}
                and payload.get("erp_target_fingerprint") == digest({"mode": self.erp.mode, "url": self.settings.erp_url})
                and payload.get("snapshot_hash") == digest({k: v for k, v in payload.items() if k != "snapshot_hash"}))

    def verify_operation(self, principal, operation_id):
        """Independently read the ERP now. Never dispatch, retry, or alter business status.

        An earlier COMPLETED record is historical; this receipt detects later ERP
        drift without rewriting that record. Any workspace reader can verify.
        """
        with self.transaction(principal) as session:
            operation = session.scalar(select(OperationRow).where(
                OperationRow.id == operation_id, OperationRow.tenant_id == principal.tenant_id))
            if operation is None:
                raise DomainError("NOT_FOUND", "Operation not found in this workspace", 404)
            payload = dict(operation.payload)
            request_id, expected_id = operation.request_id, operation.remote_id
            historical_status = operation.status
        receipt = {"operation_id": operation_id, "snapshot_hash": payload["snapshot_hash"],
                   "operation_status": historical_status, "verified_at": now(),
                   "status": "unavailable", "matches_snapshot": False, "draft_verified": False,
                   "network_attempted": False, "external_write_attempted": False,
                   "simulated": self.erp.mode == "mock", "remote_id": None}
        if not self._execution_target_matches(payload):
            receipt.update(status="blocked", reason="EXECUTION_TARGET_OR_SNAPSHOT_CHANGED")
        else:
            try:
                receipt["network_attempted"] = self.erp.mode != "mock"
                remote = self.erp.find(operation_id)
                if remote is None:
                    receipt.update(status="missing", reason="REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT")
                elif (remote_matches(remote, payload, operation_id)
                      and (expected_id is None or expected_id == remote["name"])):
                    receipt.update(status="verified", matches_snapshot=True, draft_verified=True, remote_id=remote["name"])
                else:
                    receipt.update(status="mismatch", reason="REMOTE_PAYLOAD_MISMATCH")
            except ERPRejected:
                receipt.update(status="mismatch", reason="ERP_DOCUMENT_REJECTED")
            except Exception:
                # Do not persist arbitrary upstream errors, URLs, credentials or document prose.
                receipt.update(status="unavailable", reason="ERP_VERIFICATION_UNAVAILABLE")
        with self.transaction(principal, write=True) as session:
            self._request(session, principal, request_id, lock=True)
            audit(session, principal, request_id, "ERP_VERIFICATION_" + receipt["status"].upper(), receipt)
        return receipt

    def _validate_execution_authority(self, session, operation):
        # Sessions end independently of already accepted business decisions. A
        # membership change does withdraw old authority to initiate a first write.
        if self.settings.mode == "pilot" and not self.identities.actor_valid(
                session, operation.tenant_id, operation.initiator_id, "buyer",
                auth_version=operation.initiator_auth_version, lock=True):
            raise DomainError("BUYER_REVOKED", "The requesting user no longer has execution permission", 403)

    def _assert_dispatch_deadlines(self, session, operation, approval, principal, trusted_worker):
        """Use one final clock sample after all potentially slow identity reads.

        The corresponding authority rows are already locked and validated. This
        final step covers a buyer expiring while approval/source/policy validation
        runs, or either member expiring while the other is being checked.
        """
        # A scheduled higher revision can become effective without any writer.
        # Capture its earliest activation while the tenant lock prevents changes,
        # then compare it with the same final clock used for identity deadlines.
        policy_deadline = session.scalar(select(PolicyVersionRow.effective_at).where(
            PolicyVersionRow.tenant_id == operation.tenant_id,
            PolicyVersionRow.version > operation.payload["policy_version"])
            .order_by(PolicyVersionRow.effective_at).limit(1))
        members, caller = [], None
        if self.settings.mode == "pilot":
            users = [operation.initiator_id, approval.approver_id]
            if not trusted_worker:
                users.append(principal.user_id)
            members = list(session.scalars(select(PilotMembershipRow).where(
                PilotMembershipRow.tenant_id == operation.tenant_id,
                PilotMembershipRow.user_id.in_(users))
                .execution_options(populate_existing=True)))
            if not trusted_worker:
                caller = session.get(PilotSessionRow, principal.session_id)
        stamp = datetime.now(timezone.utc)
        def elapsed(value):
            if not value:
                return False
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.tzinfo is None or parsed <= stamp
            except (TypeError, ValueError):
                return True
        if elapsed(policy_deadline):
            raise DomainError("APPROVAL_STALE", "A new policy became effective before dispatch")
        for member in members:
            if elapsed(member.expires_at):
                if not trusted_worker and member.user_id == principal.user_id:
                    raise DomainError("UNAUTHENTICATED", "The calling membership expired before dispatch", 401)
                code = "BUYER_REVOKED" if member.user_id == operation.initiator_id else "APPROVER_REVOKED"
                raise DomainError(code, "A required membership expired before dispatch", 403)
        if caller is not None and elapsed(caller.expires_at):
            raise DomainError("UNAUTHENTICATED", "The session expired before dispatch", 401)
        if elapsed(approval.expires_at):
            raise DomainError("APPROVAL_EXPIRED", "The approval expired before dispatch")

    def process_operation(self, principal, operation_id):
        require(principal, "buyer")
        result = self._process_operation(principal, operation_id)
        # The external receipt has already committed. A session revoked during
        # network I/O must not receive further protected data, but must not roll
        # back the durable no-replay result either.
        with self.transaction(principal):
            pass
        return result

    def process_pending_operation(self, tenant_id, operation_id):
        """Internal outbox entry. The API never accepts a caller-supplied worker flag.

        A worker has no human session. The approving membership generation is still
        checked durably at both first-write gates; uncertain operations remain read-only.
        """
        principal = Principal(user_id="outbox-worker", tenant_id=tenant_id, role="buyer")
        return self._process_operation(principal, operation_id, trusted_worker=True)

    def _process_operation(self, principal, operation_id, trusted_worker=False):
        # For a human caller use the live session at each actionable transaction.
        # Do not bypass validation by comparing a user-controlled user_id string.
        transaction = (lambda: self.db.transaction(write=True)) if trusted_worker else (
            lambda: self.transaction(principal, write=True))
        with transaction() as session:
            operation = session.scalar(select(OperationRow).where(OperationRow.id == operation_id, OperationRow.tenant_id == principal.tenant_id))
            if not operation:
                raise DomainError("NOT_FOUND", "Operation not found in this workspace", 404)
            request = self._request(session, principal, operation.request_id, lock=True)
            session.refresh(operation)
            if operation.status == "COMPLETED":
                return op_dto(operation)
            if operation.status == "IN_FLIGHT" and operation.lease_until and operation.lease_until > now():
                return op_dto(operation)
            if not self._execution_target_matches(operation.payload):
                operation.status, operation.error = "NEEDS_HUMAN", "EXECUTION_TARGET_OR_SNAPSHOT_CHANGED"
                request.status = "NEEDS_HUMAN"
                session.scalar(select(OutboxRow).where(OutboxRow.operation_id == operation_id)).status = "DONE"
                audit(session, principal, request.id, "DISPATCH_DENIED", {"operation_id": operation_id, "reason": operation.error})
                return op_dto(operation)
            fresh = operation.status == "PENDING"
            if fresh:
                try:
                    self._validate_execution_authority(session, operation)
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
                    if not trusted_worker:
                        self.identities.validate_principal(session, principal, lock=True)
                    request = self._request(session, principal, payload["request_id"], lock=True)
                    operation = session.get(OperationRow, operation_id)
                    if operation.lease_until != lease:
                        return op_dto(operation)
                    self._validate_execution_authority(session, operation)
                    self._validate_approval(session, principal, request, session.get(ApprovalRow, operation.approval_id))
                    # Keep both locks through the external write. A concurrent policy
                    # publisher cannot commit between validation and dispatch. Future
                    # activation is evaluated immediately before this linearization point.
                    def authorize_first_write():
                        # ERP adapters may perform slow metadata reads before
                        # posting. Recheck at their actual first-write boundary.
                        self._assert_policy_current(session, request, payload["policy_hash"])
                        if not trusted_worker:
                            self.identities.validate_principal(session, principal, lock=True)
                        self._validate_execution_authority(session, operation)
                        approval = session.get(ApprovalRow, operation.approval_id)
                        if not self.identities.approver_valid(session, principal.tenant_id, approval.approver_id,
                                auth_version=approval.approver_auth_version, lock=True):
                            raise DomainError("APPROVER_REVOKED", "The approving user no longer has approval permission", 403)
                        self._assert_dispatch_deadlines(session, operation, approval, principal, trusted_worker)
                    guarded = getattr(self.erp, "create_draft_guarded", None)
                    if not callable(guarded):
                        raise ERPRejected("ERP_GUARDED_DISPATCH_REQUIRED")
                    remote = guarded(operation_id, payload, authorize_first_write)
            if remote is None:
                error = "REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT"
            elif remote_matches(remote, payload, operation_id):
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
        # Record the external result even if its initiating session is now invalid.
        # This finalization cannot dispatch or authorize another external write.
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
        with self.transaction(principal) as session:
            self._request(session, principal, request_id)
            return [{"id": row.id, "type": row.type, "actor_id": row.actor_id, "payload": row.payload, "created_at": row.created_at}
                    for row in session.scalars(select(EventRow).where(EventRow.request_id == request_id,
                        EventRow.tenant_id == principal.tenant_id, EventRow.id > after).order_by(EventRow.id).limit(1000))]
