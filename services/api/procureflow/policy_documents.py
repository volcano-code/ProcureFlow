"""Tenant-scoped, immutable reference documents. Never executable policy rules.

The default BM25 path is offline. Optional local neural embeddings are explicitly
selected; missing/mismatched indexes fail closed, never silently change modes.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select, func, and_
from sqlalchemy.orm import load_only
from .contracts import Contract
from .db import PolicyDocumentRow, PolicyDocumentRevisionRow, audit, uid
from .domain import digest
from .errors import DomainError
from .policies import lock_tenant
from .service import require

MAX_DOCUMENTS = 32
MAX_VERSIONS = 50
MAX_CHUNKS = 2048
READ_ROLES = {'buyer', 'approver', 'auditor'}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


class PublishDocument(Contract):
    expected_version: int = Field(ge=0, strict=True)
    title: str = Field(min_length=1, max_length=160)
    source_label: str = Field(min_length=1, max_length=200)
    scope: str = Field(default='procurement', pattern=r'^[a-z0-9_-]{1,40}$')
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(max_length=12000)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    withdrawn: bool = Field(default=False, strict=True)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator('content', mode='before')
    @classmethod
    def text_only(cls, value):
        if not isinstance(value, str) or any(ord(c) < 32 and c not in '\n\r\t' or 0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError('Provide plain UTF-8 text without NUL')
        return value

    @field_validator('effective_at', 'expires_at', mode='before')
    @classmethod
    def timestamp(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError('Use an explicit timezone-aware timestamp')
        return value

    @field_validator('effective_at', 'expires_at')
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError('Timezone is required')
        return value

    @model_validator(mode='after')
    def valid_body(self):
        if self.withdrawn and self.content or not self.withdrawn and not self.content.strip():
            raise ValueError('Active documents need text; withdrawals must have empty text')
        return self


class SearchDocuments(Contract):
    query: str = Field(min_length=2, max_length=300)
    scope: str = Field(default='procurement', pattern=r'^[a-z0-9_-]{1,40}$')
    limit: int = Field(default=5, ge=1, le=10, strict=True)
    method: Literal['lexical', 'hybrid'] = 'lexical'


def tokens(text: str) -> list[str]:
    """Versioned simple Chinese bigrams + Latin words, not neural semantics."""
    result = re.findall(r'[a-z0-9]+', text.casefold())
    for word in re.findall(r'[\u3400-\u9fff]+', text):
        result += [word] if len(word) == 1 else [word[i:i+2] for i in range(len(word)-1)]
    return result


def chunks(content: str) -> list[dict]:
    result = []
    # Offsets and hashes refer to this exact stored Unicode string, not a copy
    # with whitespace/markup removed. Long lines have explicit character spans.
    start = 0
    while start < len(content):
        end = min(start + 400, len(content))
        fragment = content[start:end]
        result.append({'ordinal': len(result), 'start': start, 'end': end,
            'line_start': content.count('\n', 0, start)+1,
            'line_end': content.count('\n', 0, end-1)+1,
            'text': fragment, 'sha256': sha(fragment)})
        if end == len(content):
            break
        start = end - 80
    return result


def validate_vectors(vectors, count, dimension):
    if type(vectors) is not list or len(vectors) != count or not 1 <= dimension <= 2048:
        raise DomainError('EMBEDDING_INVALID', 'Invalid local embedding dimensions', 503)
    for vector in vectors:
        if type(vector) is not list or len(vector) != dimension or any(
                type(x) not in (int, float) or not math.isfinite(x) for x in vector):
            raise DomainError('EMBEDDING_INVALID', 'Invalid local embedding values', 503)
        norm = math.sqrt(sum(x*x for x in vector))
        if not 0.999 <= norm <= 1.001:
            raise DomainError('EMBEDDING_INVALID', 'Local embeddings must be normalized', 503)
    return vectors


def revision_body(row):
    return {key: getattr(row, key) for key in ('id', 'tenant_id', 'document_id', 'version',
        'title', 'source_label', 'scope', 'content', 'source_hash', 'fragments',
        'effective_at', 'expires_at', 'withdrawn', 'created_at', 'created_by', 'reason',
        'embedding_id', 'embedding_dimension', 'embeddings')}


def check_integrity(row):
    if (sha(row.content) != row.source_hash or chunks(row.content) != row.fragments
            or digest(revision_body(row)) != row.revision_hash):
        raise DomainError('POLICY_DOCUMENT_INTEGRITY', 'Reference document integrity check failed', 503)
    if row.embedding_id:
        validate_vectors(row.embeddings, len(row.fragments), row.embedding_dimension)


SUMMARY_FIELDS = ('id', 'document_id', 'version', 'title', 'source_label', 'scope',
    'source_hash', 'effective_at', 'expires_at', 'withdrawn', 'created_at', 'created_by',
    'reason', 'embedding_id', 'embedding_dimension', 'revision_hash')


def view(row, at: str, selected_id=None, include_text=False):
    result = {key: getattr(row, key) for key in SUMMARY_FIELDS}
    result.update(advisory_only=True,
        status=('scheduled' if row.effective_at > at else 'superseded' if selected_id != row.id
            else 'withdrawn' if row.withdrawn else 'expired' if row.expires_at and row.expires_at <= at else 'active'))
    if include_text:
        result.update(content=row.content, fragments=row.fragments)
    return result



class PolicyDocumentService:
    def __init__(self, procurement, encoder=None):
        self.procurement, self.encoder = procurement, encoder

    @staticmethod
    def _read(principal):
        if principal.role not in READ_ROLES:
            raise DomainError('FORBIDDEN', 'This identity cannot read reference documents', 403)

    def _selected(self, session, principal, at):
        # Select at most one revision per document in SQL, before loading text or
        # vectors. Tenant filtering and max-version precede expiry/scope/ranking.
        latest = select(PolicyDocumentRevisionRow.document_id,
            func.max(PolicyDocumentRevisionRow.version).label('version')).where(
            PolicyDocumentRevisionRow.tenant_id == principal.tenant_id,
            PolicyDocumentRevisionRow.effective_at <= at).group_by(
            PolicyDocumentRevisionRow.document_id).subquery()
        query = select(PolicyDocumentRevisionRow).join(latest, and_(
            PolicyDocumentRevisionRow.document_id == latest.c.document_id,
            PolicyDocumentRevisionRow.version == latest.c.version)).where(
            PolicyDocumentRevisionRow.tenant_id == principal.tenant_id).order_by(
            PolicyDocumentRevisionRow.document_id).limit(MAX_DOCUMENTS+1)
        rows = list(session.scalars(query))
        if len(rows) > MAX_DOCUMENTS:
            raise DomainError('LIBRARY_LIMIT', 'Reference document limit exceeded', 422)
        for row in rows:
            check_integrity(row)
        return {row.document_id: row for row in rows}

    def publish(self, principal, document_key, command):
        require(principal, 'approver')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', document_key):
            raise DomainError('DOCUMENT_KEY_INVALID', 'Use a bounded document key', 422)
        fragments = chunks(command.content)
        embeddings, embedding_id, dimension = [], None, 0
        # Authenticate before any expensive encoding, then revalidate authority
        # and CAS under the write transaction after encoding.
        with self.procurement.transaction(principal) as session:
            pass
        if self.encoder and fragments:
            embedding_id, dimension = self.encoder.identity, self.encoder.dimension
            embeddings = validate_vectors(self.encoder.encode([x['text'] for x in fragments]), len(fragments), dimension)
        with self.procurement.transaction(principal, write=True) as session:
            lock_tenant(session, principal.tenant_id)
            document = session.scalar(select(PolicyDocumentRow).where(
                PolicyDocumentRow.tenant_id == principal.tenant_id, PolicyDocumentRow.document_key == document_key))
            latest = document.latest_version if document else 0
            if command.expected_version != latest:
                raise DomainError('VERSION_CONFLICT', 'Refresh reference history before publishing', 409)
            if latest >= MAX_VERSIONS:
                raise DomainError('LIBRARY_LIMIT', 'Reference document revision limit reached', 422)
            if document is None:
                count = session.scalar(select(func.count()).select_from(PolicyDocumentRow).where(
                    PolicyDocumentRow.tenant_id == principal.tenant_id))
                if count >= MAX_DOCUMENTS:
                    raise DomainError('LIBRARY_LIMIT', 'Reference document limit reached', 422)
                document = PolicyDocumentRow(id=uid('pd_'), tenant_id=principal.tenant_id,
                    document_key=document_key, latest_version=0)
                session.add(document)
                session.flush()
            timestamp = datetime.now(timezone.utc)
            effective = command.effective_at or timestamp
            if command.expires_at and command.expires_at <= effective:
                raise DomainError('DOCUMENT_WINDOW_INVALID', 'Expiry must be later than activation', 422)
            row = PolicyDocumentRevisionRow(id=uid('pdr_'), tenant_id=principal.tenant_id,
                document_id=document.id, version=latest+1, title=command.title,
                source_label=command.source_label, scope=command.scope, content=command.content,
                source_hash=sha(command.content), fragments=fragments, effective_at=utc(effective),
                expires_at=utc(command.expires_at) if command.expires_at else None,
                withdrawn=command.withdrawn, created_at=utc(timestamp), created_by=principal.user_id,
                reason=command.reason, embedding_id=embedding_id, embedding_dimension=dimension,
                embeddings=embeddings, revision_hash='')
            row.revision_hash = digest(revision_body(row))
            session.add(row)
            document.latest_version = row.version
            session.flush()
            audit(session, principal, None, 'POLICY_DOCUMENT_PUBLISHED', {
                'document_id': document.id, 'revision_id': row.id, 'version': row.version,
                'source_hash': row.source_hash, 'advisory_only': True})
            selected = self._selected(session, principal, utc(timestamp))
            return {**view(row, utc(timestamp), getattr(selected.get(document.id), 'id', None)),
                'document_key': document_key}

    def history(self, principal, limit=50, offset=0):
        self._read(principal)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1600:
            raise DomainError('PAGINATION_INVALID', 'Use bounded history pagination', 422)
        with self.procurement.transaction(principal, consistent=True) as session:
            at = utc(datetime.now(timezone.utc))
            chosen = self._selected(session, principal, at)
            rows = list(session.scalars(select(PolicyDocumentRevisionRow).where(
                PolicyDocumentRevisionRow.tenant_id == principal.tenant_id).options(
                load_only(*(getattr(PolicyDocumentRevisionRow, x) for x in SUMMARY_FIELDS))).order_by(
                PolicyDocumentRevisionRow.created_at.desc(), PolicyDocumentRevisionRow.id).offset(offset).limit(limit+1)))
            keys = {r.id: r.document_key for r in session.scalars(select(PolicyDocumentRow).where(
                PolicyDocumentRow.tenant_id == principal.tenant_id))}
            return {'read_at': at, 'advisory_only': True, 'next_offset': offset+limit if len(rows)>limit else None,
                'documents': [{**view(row, at, getattr(chosen.get(row.document_id), 'id', None)),
                 'document_key': keys[row.document_id]} for row in rows[:limit]]}

    def revision(self, principal, revision_id):
        self._read(principal)
        with self.procurement.transaction(principal, consistent=True) as session:
            row = session.scalar(select(PolicyDocumentRevisionRow).where(
                PolicyDocumentRevisionRow.id == revision_id,
                PolicyDocumentRevisionRow.tenant_id == principal.tenant_id))
            if row is None:
                raise DomainError('NOT_FOUND', 'Reference revision not found', 404)
            check_integrity(row)
            at = utc(datetime.now(timezone.utc))
            chosen = self._selected(session, principal, at)
            return view(row, at, getattr(chosen.get(row.document_id), 'id', None), True)

    def _snapshot(self, session, principal, scope):
        at = utc(datetime.now(timezone.utc))
        chosen = self._selected(session, principal, at)
        active = [r for r in chosen.values() if not r.withdrawn and r.scope == scope
            and (not r.expires_at or r.expires_at > at)]
        # Active set changes on activation, expiration, withdrawal or scope change.
        signature = digest([(r.id, r.revision_hash) for r in active])
        return active, signature, at

    def search(self, principal, command):
        self._read(principal)
        query_tokens = tokens(command.query)
        if not query_tokens:
            raise DomainError('QUERY_EMPTY', 'Query needs searchable words', 422)
        with self.procurement.transaction(principal, consistent=True) as session:
            active, signature, at = self._snapshot(session, principal, command.scope)
            records = [(r, f) for r in active for f in r.fragments]
        if len(records) > MAX_CHUNKS:
            raise DomainError('LIBRARY_LIMIT', 'Reference retrieval limit exceeded', 422)
        vectors = None
        if command.method == 'hybrid':
            if not self.encoder:
                raise DomainError('EMBEDDING_NOT_CONFIGURED', 'Install and configure local embeddings explicitly', 503)
            if any(r.embedding_id != self.encoder.identity or r.embedding_dimension != self.encoder.dimension for r in active):
                raise DomainError('EMBEDDING_INDEX_STALE', 'Publish new indexed revisions with the configured encoder', 409)
            vectors = validate_vectors(self.encoder.encode([command.query], query=True), 1, self.encoder.dimension)[0]
        counters = [Counter(tokens(r.title + ' ' + f['text'])) for r, f in records]
        n = len(counters)
        avg_length = sum(sum(c.values()) for c in counters) / max(1, n)
        frequencies = {t: sum(t in c for c in counters) for t in set(query_tokens)}
        lexical = []
        for i, c in enumerate(counters):
            score = 0.0
            for token in set(query_tokens):
                tf = c[token]
                if not tf:
                    continue
                df = frequencies[token]
                idf = math.log(1 + (n-df+0.5)/(df+0.5))
                score += idf * tf * 2.2 / (tf+1.2*(0.25+0.75*sum(c.values())/max(1, avg_length)))
            if score > 0:
                lexical.append((i, score))
        lexical.sort(key=lambda item: (-item[1], item[0]))
        scores = {i: 1/(60+rank) for rank, (i, _) in enumerate(lexical, 1)}
        similarities = {}
        if vectors is not None:
            similarities = {i: sum(a*b for a, b in zip(vectors, r.embeddings[f['ordinal']])) for i, (r, f) in enumerate(records)}
            semantic = sorted(similarities, key=lambda i: (-similarities[i], i))
            for rank, i in enumerate(semantic[:20], 1):
                if similarities[i] > 0:
                    scores[i] = scores.get(i, 0) + 1/(60+rank)
        ordered = sorted(scores, key=lambda i: (-scores[i], i))[:command.limit]
        results = []
        for i in ordered:
            r, f = records[i]
            results.append({'citation_id': r.id+':'+str(f['ordinal']), 'revision_id': r.id,
                'document_id': r.document_id, 'version': r.version, 'title': r.title,
                'source_label': r.source_label, 'source_hash': r.source_hash,
                'revision_hash': r.revision_hash, 'effective_at': r.effective_at,
                'expires_at': r.expires_at, 'fragment': f,
                'rank_score': scores[i], 'cosine_similarity': similarities.get(i),
                'trust': 'reference_text_not_instructions', 'advisory_only': True})
        with self.procurement.transaction(principal, consistent=True) as session:
            _, current, checked_at = self._snapshot(session, principal, command.scope)
            if current != signature:
                raise DomainError('DOCUMENT_SEARCH_STALE', 'Reference versions changed; repeat the search', 409)
        return {'method': command.method, 'encoder': self.encoder.identity if vectors is not None else None,
            'snapshot_hash': signature, 'read_at': checked_at, 'advisory_only': True,
            'status': 'matches' if results else 'no_match', 'results': results,
            'notice': 'Retrieval is not a compliance decision. Relevance scores are not confidence probabilities.'}
