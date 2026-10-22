# Versioned policy documents: evidence retrieval, not rule enforcement

This increment adds immutable document revisions, explicit publication/retirement,
tenant-filtered retrieval, verifiable source spans, a native workbench panel and
optional `search_policy(query=..., top_k=...)` through the shared Function Calling
and official MCP stdio registry. Empty `search_policy({})` retains its existing
structured-policy response. No new approve/confirm/execute tool is exposed.

## Retrieval method and honest boundaries

`bm25-char-tfidf-rrf-v1` combines lexical BM25 and exact sparse character (2/3-gram)
TF-IDF cosine rankings using reciprocal rank fusion. All corpus statistics are
computed AFTER tenant, scope, publication and effective-version filtering.
There are no neural embeddings, vector database extension, ANN index, network
embedding service or provider calls. This is a keyless retrieval baseline, not
completion of the planned learned-embedding/pgvector RAG capability. Paraphrases,
cross-language questions and semantic conflicts can be missed. Retrieval scores
are neither probabilities nor evidence of compliance.

Tokenization/index normalization never changes stored source text. Returned
chunks include exact Unicode-code-point offsets, line numbers, source SHA-256,
chunk hash, immutable version ID and corpus hash. Display source text as text,
not executable Markdown/HTML. Documents remain untrusted data, not instructions.

The enforcing `tenant_policies`/`policy_versions` and proposal/approval rules are
unchanged. Publishing narrative text does NOT change a spending limit or authorize
execution. Operators must use the existing structured-policy publication workflow
for executable rules. Conflicting documents require human review.

## Database and lifecycle

Migration `b51d09e37a24` adds `knowledge_documents` and `knowledge_versions` after
`a40c72de1902`. Stop writers, take a backup with the existing version, migrate with
`python -m alembic upgrade head` from `services/api`, then run `/ready`. There is no
automatic production DDL. A nonempty document corpus prevents downgrade with
`KNOWLEDGE_DATA_DOWNGRADE_REFUSED`; do not delete evidence to make downgrade pass.
Restore an old-format backup using its matching application version first, then
migrate the recovered database. Existing backup/export uses ORM metadata tables;
new document text is held in the database, not arbitrary filesystem paths.

Only an authenticated approver may append a draft or publish/retire a revision.
The document family's current version is checked under the existing tenant lock.
Text cannot be edited or deleted through this API. A family cannot change scope.
Publication accepts timezone-aware activation/expiry dates and forbids backdating.
The highest revision which has reached activation wins. If it expires or is
retired, an older version does NOT silently reactivate. A higher future version
leaves the currently active version visible until its activation time.

Search uses the current instant only. Historical metadata is available to the
approver, but a stale/retired source cannot be served through the current-citation
endpoint. Every search/citation/action revalidates existing business identity.
There are at most 64 families and 512 revisions per tenant, 32,000 characters /
64,000 UTF-8 bytes per source, and 120,000 bytes of active source context. Exceeding
a bound fails explicitly; no silent truncation. Exact scanning is for a bounded
small corpus; no large-scale latency or throughput claim is made.

## API

- `GET /api/v1/knowledge/documents`: current metadata; approvers also see history.
- `POST /api/v1/knowledge/versions`: `document_key`, `expected_version`, `title`,
  `text`, optional `scope` (default `procurement`). Creates DRAFT only.
- `POST /api/v1/knowledge/versions/{id}/publish`: `expected_document_version`,
  optional `effective_at` and `expires_at`. Explicit separate action.
- `POST /api/v1/knowledge/versions/{id}/retire`: `expected_document_version`.
- `GET /api/v1/knowledge/search?q=...&scope=procurement&top_k=5`.
- `GET /api/v1/knowledge/citations/{id}?corpus_hash=...&scope=procurement`.

A content change/activation/retirement/expiry changes the active corpus binding.
The advisory snapshot captures current documents and becomes stale when that
context changes. A pending draft does not change the active binding. Model
citations are only eligible after the corresponding search result was read.
An ID/offset/hash check is NOT semantic factuality evaluation.

## Native UI

The workbench includes "制度文档与证据检索". An approver opens version management,
pastes UTF-8 text or loads TXT/Markdown, saves a draft, then explicitly publishes.
Search returns source excerpts; "重新核验引用" checks the current corpus again.
The panel aborts abandoned requests on identity changes and does not retry writes.
Existing read-only advice citations can open these sources with a fresh API check.
Scheduling and nondefault scope are currently API features, not UI controls.
Scanned/PDF/Office policy-document ingestion is not part of this increment.

## Verification

Local, dependency-light service and migration tests:

```bash
python -m pytest -q tests/test_knowledge.py tests/test_knowledge_migration.py tests/test_mcp_bridge.py
npm run test:unit --prefix apps/web
python scripts/evaluate_knowledge.py --output /tmp/pf-knowledge-result-unique.json
```

The dedicated workflow installs the pinned application and MCP dependencies,
executes the full API and actual SDK/subprocess protocol tests, and repeats API
lifecycle/concurrency tests on PostgreSQL 17 with no SQLite fallback. The existing
native Next gate includes a new create-draft/publish/search/citation/retire browser
case. All results must be bound to the final tree; old CI does not certify changes.

`evals/knowledge` is 15 synthetic revisions and 20 public developer-authored Dev
queries (17 answerable, 3 unrelated). Do not call it unseen or independently
annotated. The first actual local run retrieved the expected family at top-5 in
14/17 cases (82.35%); all 3 unrelated queries returned no hits. Three colloquial
paraphrases failed. This does not meet the previously proposed 90% retrieval goal;
retain those failures, and evaluate a learned retriever before claiming semantic
RAG quality. The benchmark exits successfully on completed measurement, not on
all tasks passing. No paid LLM/embedding call or user-study result is claimed.
