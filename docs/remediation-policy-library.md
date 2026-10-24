# Versioned reference document retrieval

## Scope and authority

This is a **reference library**, not a new policy engine. It adds immutable plain-text
reference revisions, tenant filtering, validity windows, source fragments, retrieval
and a native workbench panel. Existing deterministic policy publication, approvals,
MCP `search_policy` snapshot reads and ERP execution stay unchanged. A retrieved
passage cannot approve, execute, modify a confirmed field or grant a role. Reference
publication does not silently invalidate or rewrite existing executable rules.

The retrieval layer is not an LLM answer generator. It does not yet inject reference
results into the existing advice/MCP tool loop. Historical raw text is readable by
an authorized identity, clearly marked; only current valid versions are ranked.

## Versions and evidence

An approver publishes `/api/v1/policy-library/documents/{key}/versions` with an
explicit `expected_version` (0 for a new key). Publication appends a new revision;
old text cannot be edited or deleted through the service. The tenant lock serializes
competing publications and the version check rejects stale writes. Future-effective
versions are supported. Selection chooses the highest version whose activation time
has arrived, *then* applies expiry, withdrawal and scope. An expired/withdrawn or
scope-changed revision therefore cannot resurrect an older revision. Withdrawals are
new empty revisions, not deletions. The latest history contains scheduled versions,
so publishing clients must review history before choosing `expected_version`.

Text is stored verbatim, including whitespace and line breaks. Sources are split into
400-code-point spans with 80-code-point overlap. Results return source SHA-256,
revision hash, immutable revision ID, character offsets and line numbers. Whole-source
and fragment integrity are rechecked before disclosure. No source URL is fetched.
Reference content is treated as data, including apparent instructions embedded in it.

Search validates the existing business identity before retrieving documents and
again before returning results. It also rechecks the active revision set after ranking.
A version change/expiry during inference returns a conflict instead of stale evidence.
No search cache bypasses session or tenant authorization.

## Search modes

* `lexical` (default): local BM25 with Latin word tokens and Chinese character bigrams.
  This is explicitly a lexical baseline, not semantic embeddings.
* `hybrid`: normalized local BGE neural vectors with exact cosine scoring plus lexical
  reciprocal-rank fusion. Set `PF_POLICY_EMBEDDING_DIR` to an operator-provisioned
  local directory **before publishing indexed revisions**. An absent encoder or
  mismatched index fails explicitly; it does not silently fall back to lexical search.

Vectors are stored as JSON alongside immutable revisions in the application database
and searched exactly over a bounded active corpus. **This batch does not implement
pgvector/ANN acceleration**. Limits are 32 documents/tenant, 50 revisions/document,
12000 characters/revision, 2048 active chunks, query length 300 and at most 10 hits.
History is paginated, 50 entries by default, at most 100. This design is for small
reference collections, not a benchmark of large-scale retrieval throughput.

Ranking values are not calibrated relevance probabilities, and a returned passage
is not proof that the user's action is compliant. Conflicting documents with different
keys may both appear; the service does not make an unsupported conflict-resolution
or regulatory judgment.

## Provision a local model without any model API key

Install API dependencies in a virtual environment, then the optional encoder:

```bash
python -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r services/api/requirements-retrieval.txt
python scripts/prepare_policy_model.py --allow-public-download \
  --output "$HOME/.local/share/procureflow/bge-policy-model"
export PF_POLICY_EMBEDDING_DIR="$HOME/.local/share/procureflow/bge-policy-model"
```

Provisioning explicitly downloads public MIT-licensed `BAAI/bge-small-zh-v1.5` at
revision `7999e1d3359715c523056ef9478215996d62a620`; it uses no Hub/API token and
excludes pickle weight files. Application inference is `local_files_only=True`,
`trust_remote_code=False`, safetensors-only, CPU inference with normalized vectors.
An overlong tokenizer input is rejected rather than silently truncated. Keep the
operator-owned model directory immutable while the service is running. Model files
are not bundled in project releases. SentenceTransformers/Transformers/Torch are
optional; lexical search does not import them.

There is no claim here that a local encoder is free of hardware/installation cost.
It avoids a paid inference API, not all resource consumption. The local network may
block provisioning; that must remain a blocked test, not simulated learned inference.

## Native workbench

Open **租户采购策略 → 制度参考文档：检索、原文与版本**. All authenticated roles can
search/read; only the approver sees publication controls. New documents use version 0;
new revisions use the latest published version. Unknown publication outcomes or version
conflicts block another submit until history is reread. No automatic write retry occurs.
Content is rendered as escaped plain text, not HTML. Switching sessions aborts outstanding
requests and clears previously displayed references. Publishing a reference does not
change executable budget rules: use the existing separate policy workflow for that.

## API contracts and migration

The library is a mounted FastAPI sub-application under `/api/v1/policy-library`.
Its OpenAPI document is separately authenticated at that prefix's `/openapi.json`;
the existing aggregate business OpenAPI remains unchanged. Export/check its schema:

```bash
python scripts/export_policy_contract.py --check
```

For any non-disposable database, back up first and stop API/workers before upgrading:

```bash
cd services/api
python -m alembic upgrade head
```

Revision `b719aa310001` follows `a40c72de1902` and creates two reference tables.
Downgrade refuses to discard reference history or active candidate dispatch. Restore
old backups with the corresponding old application/schema, then migrate; do not
bypass the existing backup schema guard. No existing database is changed merely by
receiving this source package. PostgreSQL still requires explicit Alembic migration.

## Executable verification and honest limits

```bash
python -m pytest -q tests/test_policy_library.py tests/test_policy_library_migration.py
python scripts/evaluate_policy_search.py --output /tmp/pf-reference-lexical.json
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python scripts/evaluate_policy_search.py \
  --model-dir "$PF_POLICY_EMBEDDING_DIR" --output /tmp/pf-reference-hybrid.json
python scripts/verify_next.py --reference-library --output /tmp/pf-reference-browser
```

The 12 documents and 20 queries are synthetic developer-authored retrieval examples,
not a hidden test set or legal/enterprise documents. Recall@5 is measured independently
for lexical/hybrid and includes failed queries. Unit fixture vectors prove protocol
handling only, never learned semantic quality. The neural CI must execute the public
model; failure to download/load is not replaced by hashes or canned embeddings.
This batch does not complete AG-UI, remote MCP OAuth, real LLM procurement evaluation,
independent human semantic review, external user research or a production release.
