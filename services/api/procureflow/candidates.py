"""Version-bound advisory extraction. No auto-replay, auto-confirmation or ERP tools."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from sqlalchemy import select, func
from .advice import SAFE_ERRORS
from .candidate_contracts import CandidateProposal, FIELDS
from .contracts import QuoteValues
from .db import DocumentRow, QuoteRow, QuoteVersionRow, QuoteCandidateRunRow, audit, now, uid
from .domain import digest, offer_check
from .errors import DomainError
from .policies import effective_policy
from .service import require
from .tabular import _run_worker

TERMINAL = {'NEEDS_REVIEW', 'NEEDS_CLARIFICATION', 'IMPORTED', 'CANCELLED', 'FAILED', 'STALE', 'INTERRUPTED'}
ERRORS = SAFE_ERRORS | {'CANDIDATE_MODEL_DISABLED', 'CANDIDATE_INPUT_CHANGED', 'CANDIDATE_INPUT_CORRUPTED',
    'CANDIDATE_FAILED', 'CANDIDATE_INTERRUPTED', 'CANDIDATE_OUTPUT_INVALID'}


class CandidateService:
    def __init__(self, procurement):
        self.p = procurement
        self.db = procurement.db

    def _document(self, session, principal, document_id, request_id):
        row = session.scalar(select(DocumentRow).where(DocumentRow.id == document_id,
            DocumentRow.tenant_id == principal.tenant_id, DocumentRow.request_id == request_id))
        if row is None:
            raise DomainError('NOT_FOUND', 'Candidate source not found in this workspace', 404)
        self.p._verify_document(row)
        return row

    def _run(self, session, principal, run_id, lock=False):
        query = select(QuoteCandidateRunRow).where(QuoteCandidateRunRow.id == run_id,
            QuoteCandidateRunRow.tenant_id == principal.tenant_id).execution_options(populate_existing=True)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise DomainError('NOT_FOUND', 'Candidate run not found in this workspace', 404)
        return row

    def _capture(self, session, principal, request, doc):
        if request.data.get('lines') is not None:
            raise DomainError('CANDIDATE_SCOPE_UNSUPPORTED', 'Use explicit tabular mapping for multi-item procurement', 422)
        self.p._mutable(request)
        return {'request_id': request.id, 'request_version': request.version, 'request': deepcopy(request.data),
                'policy': effective_policy(session, principal.tenant_id),
                'document_id': doc.id, 'document_sha256': doc.sha256, 'fragments_hash': digest(doc.fragments),
                'generation': self.db.state()['generation'], 'contract': 'quote-candidate-v1'}

    def _fresh(self, session, principal, request, row):
        if digest(row.input_snapshot) != row.input_hash:
            return False, 'CANDIDATE_INPUT_CORRUPTED'
        try:
            doc = self._document(session, principal, row.document_id, request.id)
            same = digest(self._capture(session, principal, request, doc)) == row.input_hash
            return same, None if same else 'CANDIDATE_INPUT_CHANGED'
        except (DomainError, OSError):
            return False, 'CANDIDATE_INPUT_CHANGED'

    def _view(self, session, principal, row, request):
        current, reason = self._fresh(session, principal, request, row)
        timed_out = (row.status == 'RUNNING' and (not row.lease_until or row.lease_until <= now()))
        expired = row.expires_at <= now()
        status = 'INTERRUPTED' if timed_out else row.status
        source = session.get(DocumentRow, row.document_id)
        output_ok = not row.output or row.output.get('output_hash') == digest(
            {k:v for k,v in row.output.items() if k != 'output_hash'})
        if not output_ok:
            current, reason = False, 'CANDIDATE_OUTPUT_CORRUPTED'
        return {'id': row.id, 'request_id': row.request_id, 'document_id': row.document_id,
                'filename': source.filename, 'revision': row.revision, 'status': status, 'stored_status': row.status,
                'input_hash': row.input_hash, 'output': deepcopy(row.output) if output_ok else None, 'quote_id': row.quote_id,
                'current': current, 'expired': expired, 'stale_reason': reason,
                'error_code': 'CANDIDATE_INTERRUPTED' if timed_out else row.error_code,
                'can_import': current and not expired and row.status in {'NEEDS_REVIEW', 'NEEDS_CLARIFICATION'},
                'created_at': row.created_at, 'completed_at': row.completed_at, 'expires_at': row.expires_at,
                'advisory_only': True, 'semantic_factuality_verified': False, 'auto_confirmed': False}

    def upload(self, principal, request_id, filename, data):
        require(principal, 'buyer')
        if not data or len(data) > self.p.settings.max_upload_bytes:
            raise DomainError('UPLOAD_LIMIT', 'Empty or oversized candidate source', 413)
        filename = Path(filename.replace('\\', '/')).name[:160]
        if Path(filename).suffix.lower() not in {'.txt', '.pdf'}:
            raise DomainError('UNSUPPORTED_FILE', 'Use TXT or text PDF; ordinary tables use the existing explicit-mapping flow', 415)
        with self.p.transaction(principal) as session:
            request = self.p._request(session, principal, request_id)
            self.p._mutable(request)
            if request.data.get('lines') is not None:
                raise DomainError('CANDIDATE_SCOPE_UNSUPPORTED', 'Single-item candidate extraction only', 422)
        document_id = uid('doc_')
        fragments = _run_worker('fragments', data, filename, document_id)['fragments']
        sha = hashlib.sha256(data).hexdigest()
        path = self.p.document_dir / (document_id + Path(filename).suffix.lower())
        created = False
        try:
            with self.p.transaction(principal, write=True) as session:
                request = self.p._request(session, principal, request_id, lock=True)
                self.p._mutable(request)
                if request.data.get('lines') is not None:
                    raise DomainError('CANDIDATE_SCOPE_UNSUPPORTED', 'Request changed to a multi-item basket', 409)
                doc = session.scalar(select(DocumentRow).where(DocumentRow.tenant_id == principal.tenant_id,
                    DocumentRow.request_id == request_id, DocumentRow.sha256 == sha))
                if doc is None:
                    count = session.scalar(select(func.count()).select_from(DocumentRow).where(
                        DocumentRow.tenant_id == principal.tenant_id, DocumentRow.request_id == request_id))
                    if count >= 20:
                        raise DomainError('CANDIDATE_SOURCE_LIMIT', 'At most 20 sources per request', 413)
                    with path.open('xb') as stream:
                        created = True
                        path.chmod(0o600)
                        stream.write(data)
                    doc = DocumentRow(id=document_id, tenant_id=principal.tenant_id, request_id=request_id,
                        filename=filename, sha256=sha, storage_key=path.name, fragments=fragments)
                    session.add(doc);session.flush()
                    audit(session, principal, request_id, 'CANDIDATE_SOURCE_UPLOADED', {'document_id': doc.id, 'sha256': sha})
                self.p._verify_document(doc)
                return {'id': doc.id, 'filename': doc.filename, 'sha256': doc.sha256,
                        'fragments': doc.fragments, 'trust': 'untrusted_source_content', 'quote_created': False}
        except Exception:
            if created:path.unlink(missing_ok=True)
            raise

    def reserve(self, principal, request_id, command):
        require(principal, 'buyer')
        with self.p.transaction(principal, write=True) as session:
            request = self.p._request(session, principal, request_id, lock=True)
            doc = self._document(session, principal, command.document_id, request_id)
            captured = self._capture(session, principal, request, doc)
            if request.version != command.expected_version:
                raise DomainError('VERSION_CONFLICT', 'Refresh the changed request', 409)
            existing = session.scalar(select(QuoteCandidateRunRow).where(
                QuoteCandidateRunRow.tenant_id == principal.tenant_id, QuoteCandidateRunRow.request_id == request_id,
                QuoteCandidateRunRow.idempotency_key == command.idempotency_key))
            if existing:
                if existing.input_hash != digest(captured) or existing.creator_id != principal.user_id:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 'Key is already bound to different candidate inputs or actor', 409)
                return self._view(session, principal, existing, request)
            if session.scalar(select(QuoteVersionRow.id).where(QuoteVersionRow.document_id == doc.id)):
                raise DomainError('SOURCE_ALREADY_IMPORTED', 'This source already has a quote; edit it with a reason', 409)
            count = session.scalar(select(func.count()).select_from(QuoteCandidateRunRow).where(
                QuoteCandidateRunRow.tenant_id == principal.tenant_id, QuoteCandidateRunRow.expires_at > now()))
            if count >= 100:
                raise DomainError('CANDIDATE_RUN_LIMIT', 'Workspace candidate run quota reached', 429)
            row = QuoteCandidateRunRow(id=uid('can_'), tenant_id=principal.tenant_id, request_id=request_id,
                document_id=doc.id, creator_id=principal.user_id, idempotency_key=command.idempotency_key,
                input_snapshot=captured, input_hash=digest(captured), status='PENDING', revision=1,
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
            session.add(row);session.flush()
            audit(session, principal, request_id, 'CANDIDATE_RESERVED', {'run_id':row.id, 'input_hash':row.input_hash})
            return self._view(session, principal, row, request)

    def get(self, principal, run_id):
        with self.p.transaction(principal, consistent=True) as session:
            row = self._run(session, principal, run_id)
            request = self.p._request(session, principal, row.request_id, lock=True)
            return self._view(session, principal, row, request)

    def list(self, principal, request_id):
        with self.p.transaction(principal, consistent=True) as session:
            request = self.p._request(session, principal, request_id, lock=True)
            rows = session.scalars(select(QuoteCandidateRunRow).where(QuoteCandidateRunRow.tenant_id == principal.tenant_id,
                QuoteCandidateRunRow.request_id == request_id).order_by(QuoteCandidateRunRow.created_at.desc()).limit(20))
            return [self._view(session, principal, row, request) for row in rows]

    def _finish(self, session, principal, row, status, error=None):
        row.status, row.error_code, row.completed_at = status, error, now()
        row.revision += 1
        row.lease_until = None
        audit(session, principal, row.request_id, 'CANDIDATE_' + status, {'run_id': row.id, 'code':error})

    def cancel(self, principal, run_id, command):
        require(principal, 'buyer')
        with self.p.transaction(principal, write=True) as session:
            row = self._run(session, principal, run_id)
            request = self.p._request(session, principal, row.request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            if row.creator_id != principal.user_id:
                raise DomainError('FORBIDDEN', 'Only the candidate initiator can cancel its run', 403)
            if row.revision != command.expected_revision:
                raise DomainError('VERSION_CONFLICT', 'Refresh the candidate run', 409)
            if row.status in {'PENDING', 'RUNNING'}:
                self._finish(session, principal, row, 'CANCELLED')
            return self._view(session, principal, row, request)

    def process(self, principal, run_id, command, factory):
        require(principal, 'buyer')
        with self.db.activity(write=True):
            return self._process(principal, run_id, command, factory)

    def _process(self, principal, run_id, command, factory):
        with self.p.transaction(principal, write=True) as session:
            row = self._run(session, principal, run_id)
            request = self.p._request(session, principal, row.request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            if row.creator_id != principal.user_id:
                raise DomainError('FORBIDDEN', 'Only the run initiator can start a model call', 403)
            if row.status == 'RUNNING' and (not row.lease_until or row.lease_until <= now()):
                self._finish(session, principal, row, 'INTERRUPTED', 'CANDIDATE_INTERRUPTED')
            if row.status != 'PENDING':
                return self._view(session, principal, row, request)
            if row.revision != command.expected_revision:
                raise DomainError('VERSION_CONFLICT', 'Refresh the candidate run', 409)
            current, reason = self._fresh(session, principal, request, row)
            if not current or row.expires_at <= now():
                self._finish(session, principal, row, 'STALE', reason or 'CANDIDATE_INPUT_CHANGED')
                return self._view(session, principal, row, request)
            snapshot = deepcopy(row.input_snapshot)
            doc = self._document(session, principal, row.document_id, request.id)
            fragments = deepcopy(doc.fragments)
            row.status = 'RUNNING';row.revision += 1
            row.lease_until = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
            audit(session, principal, request.id, 'CANDIDATE_STARTED', {'run_id':row.id, 'advisory_only':True})
            request_id = request.id

        def cancelled():
            try:
                with self.p.transaction(principal, consistent=True) as session:
                    req = self.p._request(session, principal, request_id, lock=True)
                    row = self._run(session, principal, run_id)
                    current, _ = self._fresh(session, principal, req, row)
                    return not current or row.status != 'RUNNING' or row.lease_until <= now()
            except DomainError:
                return True

        def invoke(name, arguments):
            if cancelled():raise DomainError('MODEL_CANCELLED', 'Candidate run is no longer current', 409)
            if name == 'get_request':
                return {'request': snapshot['request'], 'policy': snapshot['policy']}
            if name == 'get_source_index':
                return {'document_id': snapshot['document_id'], 'sha256':snapshot['document_sha256'],
                        'fragments': [{'id': f['id'], 'length':len(f['text'])} for f in fragments]}
            if name == 'get_evidence':
                start, limit = arguments.get('start', 0), arguments.get('limit', 16)
                return {'id':snapshot['document_id'], 'sha256':snapshot['document_sha256'],
                        'fragments': fragments[start:start+limit],
                        'next_start': start+limit if start+limit < len(fragments) else None}
            raise DomainError('TOOL_POLICY_DENIED', 'No such read-only candidate tool', 403)

        def observe(event):
            with self.p.transaction(principal, write=True) as session:
                audit(session, principal, request_id, 'CANDIDATE_AGENT_' + event['type'].upper(), {**event, 'run_id':run_id})

        agent, output, error = None, None, None
        try:
            agent = factory();agent.cancelled = cancelled
            raw = agent.run(invoke, {x['id'] for x in fragments}, observer=observe)
            proposal = CandidateProposal.model_validate({k:raw[k] for k in CandidateProposal.model_fields if k in raw})
            by_id = {f['id']:f for f in fragments}
            if not set(proposal.evidence_ids).issubset(by_id):
                raise DomainError('EVIDENCE_NOT_FOUND', 'Candidate references are outside this bound source', 502)
            output = proposal.model_dump(mode='json')
            # Persist explicit locator provenance, not untrusted model-written locators.
            output['evidence'] = {field: [{'fragment_id':ref, **by_id[ref]['locator'], 'text':by_id[ref]['text'],
                'document_id':snapshot['document_id'], 'document_sha256':snapshot['document_sha256'],
                'origin':'model_candidate'} for ref in refs] for field, refs in proposal.references.items()}
            check = offer_check(snapshot['request'], output['values'], False, snapshot['policy'])
            output['calculation'] = check
            missing = set(check['missing']) | {x for x in ('supplier_id','sku','uom','delivery_days') if getattr(proposal.values,x) is None}
            asked = {x['field'] for x in output['clarifications']}
            output['clarifications'] += [{'field':k,'question':f'原文未明确 {k}，请补充来源并核对后再确认。','reason':'missing'}
                for k in sorted(missing-asked)]
            for key in ('runtime','runtime_version','model_calls','provider_attempts','tool_calls','usage','usage_complete','usage_source','cost'):
                output[key] = raw.get(key)
            output.update(advisory_only=True, semantic_factuality_verified=False, llm_used=True)
            output['model'] = getattr(agent, 'model', None)
            output['output_hash'] = digest(output)
        except DomainError as exc:
            error = exc.code if exc.code in ERRORS else 'CANDIDATE_FAILED'
        except Exception:
            error = 'CANDIDATE_OUTPUT_INVALID'
        finally:
            if agent is not None:
                try:agent.client.close()
                except Exception:error = 'CANDIDATE_FAILED'
        # Preserve terminal receipts after revocation; do not replay a provider call.
        with self.db.transaction(write=True) as session:
            authorized = True
            try:
                self.p.identities.validate_principal(session, principal, lock=True)
            except DomainError:
                authorized = False
            req = self.p._request(session, principal, request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            current, reason = self._fresh(session, principal, req, row)
            if row.status == 'RUNNING':
                if not authorized:self._finish(session, principal, row, 'FAILED', 'MODEL_CANCELLED')
                elif not current:self._finish(session, principal, row, 'STALE', reason)
                elif row.lease_until <= now():self._finish(session, principal, row, 'INTERRUPTED','CANDIDATE_INTERRUPTED')
                elif error:self._finish(session, principal, row, 'FAILED', error)
                else:
                    row.output = output
                    self._finish(session, principal, row, 'NEEDS_CLARIFICATION' if output['clarifications'] else 'NEEDS_REVIEW')
        return self.get(principal, run_id)

    def import_quote(self, principal, run_id, command):
        require(principal, 'buyer')
        with self.p.transaction(principal, write=True) as session:
            row = self._run(session, principal, run_id)
            req = self.p._request(session, principal, row.request_id, lock=True)
            row = self._run(session, principal, run_id, lock=True)
            if row.status == 'IMPORTED':
                q, version = self.p._quote(session, principal, row.quote_id)
                return self.p._quote_view(session, q, version, req)
            if row.revision != command.expected_revision:
                raise DomainError('VERSION_CONFLICT', 'Refresh the candidate revision', 409)
            if not self._view(session, principal, row, req)['can_import']:
                raise DomainError('CANDIDATE_NOT_IMPORTABLE', 'Candidate is stale, expired, incomplete or no longer mutable', 409)
            if session.scalar(select(QuoteVersionRow.id).where(QuoteVersionRow.document_id == row.document_id)):
                raise DomainError('SOURCE_ALREADY_IMPORTED', 'This source already has a quote', 409)
            proposal = CandidateProposal.model_validate({k:row.output[k] for k in CandidateProposal.model_fields if k in row.output})
            # The server-added questions are still valid proposal fields. No value is filled here.
            q = QuoteRow(id=uid('quo_'), tenant_id=principal.tenant_id, request_id=req.id, current_version=1)
            session.add(q);session.flush()
            evidence = {field:{**refs[0], 'sources':refs, 'candidate_run_id':row.id,
                        'review_reason':command.reason} for field, refs in row.output['evidence'].items()}
            version = QuoteVersionRow(id=uid('qv_'), quote_id=q.id, document_id=row.document_id,
                version=1, values=proposal.values.model_dump(mode='json'), evidence=evidence,
                issues=['MODEL_CANDIDATE_REQUIRES_HUMAN_CONFIRMATION'], confirmed_by=None)
            session.add(version);session.flush()
            row.quote_id = q.id
            self._finish(session, principal, row, 'IMPORTED')
            self.p._invalidate(session, req)
            audit(session, principal, req.id, 'CANDIDATE_IMPORTED_UNCONFIRMED', {'run_id':row.id, 'quote_id':q.id,
                'reason':command.reason, 'confirmed':False})
            return self.p._quote_view(session, q, version, req)
