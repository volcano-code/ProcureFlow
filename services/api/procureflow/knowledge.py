"""Tenant-scoped, immutable source documents and keyless lexical retrieval.

This module is the evidence retrieval layer, not the policy enforcement engine.
TF-IDF vectors here are exact sparse character features, NOT learned embeddings.
No provider, URL fetch, code execution, or procurement write is performed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import math
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .db import KnowledgeDocumentRow, KnowledgeVersionRow, audit, uid
from .domain import digest
from .errors import DomainError
from .policies import lock_tenant
from .service import require

METHOD = "bm25-char-tfidf-rrf-v1"
MAX_FAMILIES = 64
MAX_VERSIONS = 512
MAX_CORPUS_BYTES = 120000
MAX_TEXT_BYTES = 64000
CHUNK_SIZE = 800


def clock() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NewDocumentVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    expected_version: int = Field(ge=0, strict=True)
    title: str = Field(min_length=1, max_length=160)
    scope: str = Field(default="procurement", pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    text: str = Field(min_length=1, max_length=32000)

    @field_validator("title", "text")
    @classmethod
    def usable_text(cls, value):
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Nonempty bounded UTF-8 text is required")
        return value  # Keep original whitespace and code-point offsets.


class DocumentTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_document_version: int = Field(ge=1, strict=True)


class PublishDocument(DocumentTransition):
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("effective_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("An explicit timezone is required")
        return value


def chunks_for(text: str, version_id: str) -> list[dict]:
    """Exact slices, including long lines; offsets count Unicode code points."""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start + CHUNK_SIZE // 2:
                end = newline + 1
        piece = text[start:end]
        chunks.append({"id": f"{version_id}:{len(chunks)}", "start": start, "end": end,
            "line_start": text.count("\n", 0, start) + 1,
            "line_end": text.count("\n", 0, max(start, end - 1)) + 1,
            "text": piece, "sha256": sha(piece)})
        start = end
    return chunks


def immutable_record(row) -> dict:
    return {"id": row.id, "tenant_id": row.tenant_id, "document_id": row.document_id,
        "version": row.version, "title": row.title, "scope": row.scope,
        "content_hash": row.content_hash, "chunks": row.chunks,
        "created_at": row.created_at, "created_by": row.created_by}


def checked_record(row) -> dict:
    body = immutable_record(row)
    if sha(row.text) != row.content_hash or row.chunks != chunks_for(row.text, row.id) or digest(body) != row.record_hash:
        raise DomainError("KNOWLEDGE_INTEGRITY", "Document source integrity check failed", 409)
    return body


def metadata(row, family, current: bool = False) -> dict:
    return {"id": row.id, "document_id": row.document_id, "document_key": family.document_key,
        "version": row.version, "latest_version": family.latest_version, "title": row.title,
        "scope": row.scope, "state": row.state, "current": current,
        "effective_at": row.effective_at, "expires_at": row.expires_at,
        "content_hash": row.content_hash, "created_at": row.created_at}


def capture_corpus(session, tenant_id: str, scope: str = "procurement") -> dict:
    """Filter tenant BEFORE computing features, scores, counts or corpus hashes.

    Highest version which has reached its start time wins per family. Retired or
    expired winners suppress the family; never resurrect a superseded old rule.
    Caller holds the tenant lock so publish/retire cannot interleave this read.
    """
    at = timestamp(clock())
    pairs = list(session.execute(select(KnowledgeVersionRow, KnowledgeDocumentRow).join(
        KnowledgeDocumentRow, KnowledgeVersionRow.document_id == KnowledgeDocumentRow.id
    ).where(KnowledgeVersionRow.tenant_id == tenant_id, KnowledgeDocumentRow.tenant_id == tenant_id,
        KnowledgeVersionRow.scope == scope, KnowledgeVersionRow.state.in_(["PUBLISHED", "RETIRED"]),
        KnowledgeVersionRow.effective_at <= at).order_by(
            KnowledgeVersionRow.document_id, KnowledgeVersionRow.version.desc()).limit(MAX_VERSIONS + 1)))
    if len(pairs) > MAX_VERSIONS:
        raise DomainError("KNOWLEDGE_LIMIT", "Document collection exceeds its bounded retrieval limit", 422)
    winners, documents = set(), []
    for row, family in pairs:
        if row.document_id in winners:
            continue
        winners.add(row.document_id)
        # Validate selected immutable source even when it is now retired/expired.
        checked_record(row)
        if row.state != "PUBLISHED" or (row.expires_at and row.expires_at <= at):
            continue
        documents.append({**metadata(row, family, True), "record_hash": row.record_hash,
            "chunks": row.chunks, "trust": "untrusted_source_content"})
    for doc in documents:
        doc.pop("latest_version", None)
    size = sum(len(c["text"].encode("utf-8")) for d in documents for c in d["chunks"])
    if len(documents) > MAX_FAMILIES or size > MAX_CORPUS_BYTES:
        raise DomainError("KNOWLEDGE_LIMIT", "Document context exceeds its bounded retrieval limit", 422)
    # Includes status and dates, not wall-clock time. A publication boundary changes
    # the hash; an unrelated tenant/draft does not change visible retrieval state.
    identity = [{k: d[k] for k in ("id", "document_id", "version", "content_hash", "record_hash", "effective_at", "expires_at")}
                for d in documents]
    return {"scope": scope, "documents": documents, "corpus_hash": digest(identity),
        "advisory_only": True, "method": METHOD}


def terms(text: str) -> Counter:
    text = unicodedata.normalize("NFKC", text).casefold()
    result = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text)
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(segment[i:i+2] for i in range(len(segment)-1))
        if len(segment) == 1:
            result.append(segment)
    return Counter(result)


def grams(text: str) -> Counter:
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return Counter(text[i:i+n] for n in (2, 3) for i in range(max(0, len(text)-n+1)))


def rank(corpus: dict, query: str, top_k: int = 5) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 256 or "\x00" in query:
        raise DomainError("KNOWLEDGE_QUERY", "Use a nonempty query of at most 256 characters", 422)
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise DomainError("KNOWLEDGE_QUERY", "top_k must be between 1 and 10", 422)
    entries = [(doc, chunk) for doc in corpus["documents"] for chunk in doc["chunks"]]
    result = {"method": METHOD, "advisory_only": True, "corpus_hash": corpus["corpus_hash"],
        "scope": corpus["scope"], "status": "no_effective_documents" if not entries else "no_match", "hits": []}
    if not entries:
        return result
    # Title is searchable but returned evidence is always an exact source slice.
    texts = [d["title"] + "\n" + c["text"] for d, c in entries]
    word_vectors = [terms(t) for t in texts]
    char_vectors = [grams(t) for t in texts]
    qwords, qgrams = terms(query), grams(query)
    size = len(entries)
    df = Counter(t for v in word_vectors for t in v)
    char_df = Counter(t for v in char_vectors for t in v)
    average = sum(sum(v.values()) for v in word_vectors) / max(size, 1) or 1
    idf = {t: math.log((1 + size) / (1 + count)) + 1 for t, count in char_df.items()}
    qv = {t: (1 + math.log(f)) * idf[t] for t, f in qgrams.items() if t in idf}
    qnorm = math.sqrt(sum(v*v for v in qv.values()))
    scores = []
    for i, (words, chars) in enumerate(zip(word_vectors, char_vectors)):
        length = sum(words.values())
        bm25 = sum(math.log(1 + (size - df[t] + .5)/(df[t] + .5)) *
            (words[t]*2.2)/(words[t] + 1.2*(.25 + .75*length/average))
            for t in qwords if words[t])
        cv = {t: (1 + math.log(f))*idf[t] for t, f in chars.items()}
        norm = math.sqrt(sum(v*v for v in cv.values()))
        cosine = sum(v*cv.get(t, 0) for t, v in qv.items())/(qnorm*norm) if qnorm and norm else 0
        # Empty/irrelevant character overlap alone cannot invent a positive hit.
        if bm25 > 0 or cosine >= .12:
            scores.append((i, bm25, cosine))
    word_order = {s[0]: j+1 for j, s in enumerate(sorted(scores, key=lambda x: (-x[1], entries[x[0]][1]["id"])))}
    char_order = {s[0]: j+1 for j, s in enumerate(sorted(scores, key=lambda x: (-x[2], entries[x[0]][1]["id"])))}
    fused = sorted(scores, key=lambda x: (-(1/(60+word_order[x[0]]) + 1/(60+char_order[x[0]])), entries[x[0]][1]["id"]))
    for index, bm25, cosine in fused[:top_k]:
        doc, chunk = entries[index]
        result["hits"].append({"citation_id": chunk["id"], "document_key": doc["document_key"],
            "version_id": doc["id"], "version": doc["version"], "title": doc["title"],
            "content_hash": doc["content_hash"], "text": chunk["text"],
            "chunk_hash": chunk["sha256"], "line_start": chunk["line_start"], "line_end": chunk["line_end"],
            "start": chunk["start"], "end": chunk["end"], "offset_unit": "unicode_codepoints",
            "effective_at": doc["effective_at"], "expires_at": doc["expires_at"],
            "scores": {"bm25": round(bm25, 8), "char_tfidf_cosine": round(cosine, 8)},
            "trust": "untrusted_source_content"})
    if result["hits"]:
        result["status"] = "matched"
    return result


class KnowledgeService:
    def __init__(self, procurement):
        self.procurement = procurement

    def create(self, principal, command: NewDocumentVersion):
        require(principal, "approver")
        with self.procurement.transaction(principal, write=True) as session:
            lock_tenant(session, principal.tenant_id)
            family = session.scalar(select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.tenant_id == principal.tenant_id,
                KnowledgeDocumentRow.document_key == command.document_key))
            if (family.latest_version if family else 0) != command.expected_version:
                raise DomainError("VERSION_CONFLICT", "Refresh document history before creating a revision", 409)
            count = session.scalar(select(func.count()).select_from(KnowledgeVersionRow).where(KnowledgeVersionRow.tenant_id == principal.tenant_id))
            families = session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.tenant_id == principal.tenant_id))
            if count >= MAX_VERSIONS or (family is None and families >= MAX_FAMILIES):
                raise DomainError("KNOWLEDGE_LIMIT", "Document collection limit reached", 422)
            if family is None:
                family = KnowledgeDocumentRow(id=uid("kd_"), tenant_id=principal.tenant_id,
                    document_key=command.document_key, latest_version=0)
                session.add(family); session.flush()
            else:
                first = session.scalar(select(KnowledgeVersionRow).where(KnowledgeVersionRow.document_id == family.id).limit(1))
                if first.scope != command.scope:
                    raise DomainError("KNOWLEDGE_SCOPE_FIXED", "A document family cannot change its scope", 422)
            family.latest_version += 1
            version_id = uid("kv_")
            row = KnowledgeVersionRow(id=version_id, tenant_id=principal.tenant_id,
                document_id=family.id, version=family.latest_version, title=command.title, scope=command.scope,
                text=command.text, content_hash=sha(command.text), chunks=chunks_for(command.text, version_id),
                created_at=timestamp(clock()), created_by=principal.user_id, state="DRAFT", record_hash="")
            row.record_hash = digest(immutable_record(row))
            session.add(row); session.flush()
            audit(session, principal, None, "KNOWLEDGE_DRAFT_CREATED", {"version_id": row.id, "content_hash": row.content_hash})
            return metadata(row, family)

    def transition(self, principal, version_id, command, *, publish=False):
        require(principal, "approver")
        with self.procurement.transaction(principal, write=True) as session:
            lock_tenant(session, principal.tenant_id)
            row = session.scalar(select(KnowledgeVersionRow).where(KnowledgeVersionRow.id == version_id,
                KnowledgeVersionRow.tenant_id == principal.tenant_id))
            if row is None:
                raise DomainError("NOT_FOUND", "Document version not found", 404)
            family = session.get(KnowledgeDocumentRow, row.document_id)
            if family.latest_version != command.expected_document_version:
                raise DomainError("VERSION_CONFLICT", "Refresh document history before changing publication", 409)
            checked_record(row)
            if publish:
                if row.state != "DRAFT" or row.version != family.latest_version:
                    raise DomainError("KNOWLEDGE_STATE", "Only the latest draft may be published", 409)
                at = clock(); start = command.effective_at or at
                if start < at or (command.expires_at and command.expires_at <= start):
                    raise DomainError("KNOWLEDGE_DATES", "Publication cannot be backdated and expiry must follow its start", 422)
                row.state, row.published_by = "PUBLISHED", principal.user_id
                row.effective_at = timestamp(start)
                row.expires_at = timestamp(command.expires_at) if command.expires_at else None
            else:
                if row.state != "PUBLISHED":
                    raise DomainError("KNOWLEDGE_STATE", "Only a published version may be retired", 409)
                row.state, row.retired_at = "RETIRED", timestamp(clock())
            session.flush()
            audit(session, principal, None, "KNOWLEDGE_PUBLISHED" if publish else "KNOWLEDGE_RETIRED",
                {"version_id": row.id, "content_hash": row.content_hash, "advisory_only": True})
            return metadata(row, family)

    def list(self, principal):
        with self.procurement.transaction(principal, consistent=True) as session:
            lock_tenant(session, principal.tenant_id)
            rows = list(session.execute(select(KnowledgeVersionRow, KnowledgeDocumentRow).join(
                KnowledgeDocumentRow, KnowledgeVersionRow.document_id == KnowledgeDocumentRow.id).where(
                KnowledgeVersionRow.tenant_id == principal.tenant_id, KnowledgeDocumentRow.tenant_id == principal.tenant_id
                ).order_by(KnowledgeDocumentRow.document_key, KnowledgeVersionRow.version.desc()).limit(MAX_VERSIONS + 1)))
            if len(rows) > MAX_VERSIONS:
                raise DomainError("KNOWLEDGE_LIMIT", "Document history exceeds its limit", 422)
            current = set()
            for scope in {r.scope for r, _ in rows}:
                current.update(d["id"] for d in capture_corpus(session, principal.tenant_id, scope)["documents"])
            return [metadata(r, f, r.id in current) for r, f in rows if principal.role == "approver" or r.id in current]

    def search(self, principal, query, scope="procurement", top_k=5):
        with self.procurement.transaction(principal, consistent=True) as session:
            lock_tenant(session, principal.tenant_id)
            return rank(capture_corpus(session, principal.tenant_id, scope), query, top_k)

    def citation(self, principal, citation_id, corpus_hash, scope="procurement"):
        with self.procurement.transaction(principal, consistent=True) as session:
            lock_tenant(session, principal.tenant_id)
            corpus = capture_corpus(session, principal.tenant_id, scope)
            if corpus["corpus_hash"] != corpus_hash:
                raise DomainError("KNOWLEDGE_STALE", "Document collection changed; search again", 409)
            for doc in corpus["documents"]:
                for chunk in doc["chunks"]:
                    if chunk["id"] == citation_id:
                        return {"document": {k: v for k, v in doc.items() if k != "chunks"},
                            "chunk": chunk, "corpus_hash": corpus_hash, "advisory_only": True}
            raise DomainError("NOT_FOUND", "Current citation not found in this workspace", 404)
