# Remediation batch 2 — nonstandard quote candidates

## Lineage

This branch continues the verified remote source at `9f80786179462e6f6b89b92d0dc02a58207459e1`, not the older `31065d0` package. The remote already contains pilot identities, tenant policy publication, multi-item procurement, protected recovery and a bounded LangGraph advice runtime. Those implementations are preserved, not reimplemented or counted as new work.

The previous local PR-A Dev12 dataset, independent grader and deterministic runner are imported additively. Their prior result is historical; run the measurement again against this implementation. The separate existing ten-case fixture evaluation remains separate. Neither fixture suite measures a real model.

## Implemented path

1. A buyer uploads one bounded TXT or text-PDF source to a single-item request. No quote, field confirmation or approval is created. The existing isolated parser worker produces server-owned fragments; images/OCR are not supported. Limits: configured upload bytes (default 2 MiB), 128 fragments, 16,000 extracted characters, 20 stored sources per request.
2. Reserve a run with an idempotency key. Its snapshot binds request version, published policy, file hash, fragment hash and application recovery generation. It is durable before any provider invocation.
3. Only the initiating buyer can explicitly start it. Both `PF_ALLOW_CANDIDATE_MODEL=1` and existing server `LLM_API_KEY`/`LLM_MODEL` are required. The browser also requires explicit disclosure acknowledgement. Defaults do not call a paid provider.
4. The inherited LangGraph runtime exposes only scoped `get_request`, `get_source_index` and paginated `get_evidence`. Limits default to six model requests, twelve tool calls and the existing bounded time/token/response budgets. No document ID, URL, tenant, role or write tool is accepted from the model.
5. Candidate values remain advisory. Every known field requires a reference to a fragment actually read. Unknown or ambiguous fields remain null; tax mode may be `unknown`. Source locations and source hashes come from the server, not model-written locators. Existing Decimal calculation produces a candidate total or unknown.
6. A buyer may explicitly import the result with a reason. This creates a new **unconfirmed** quote and preserves source/candidate provenance. Existing quote confirmation, proposal calculation, independent approval and execution remain mandatory. No candidate endpoint writes to ERP.

## State and reliability boundaries

States: PENDING → RUNNING → NEEDS_REVIEW / NEEDS_CLARIFICATION / FAILED / STALE / INTERRUPTED; explicit cancel and manual import have their own states. Cancelled, expired, changed, revoked or corrupted runs cannot publish/adopt a late model response. Process replay never automatically replays an already-started provider call. An imported run replays to the same quote.

The process endpoint currently waits for the bounded model call on a FastAPI worker thread. This is durable run bookkeeping, **not** an independent asynchronous Agent worker or per-tool persistent graph checkpoint. A restart preserves pending/final receipts; an expired running lease becomes INTERRUPTED, not transparently resumed. Browser navigation stops waiting only; it does not claim the server or provider was cancelled. Provider usage is reported rather than independently billed; missing usage/cost stays unknown.

The model can select additional evidence pages. This batch does not add a full conversational clarification/resume loop: missing fields are presented as questions and may be supplied through the existing versioned quote editor after importing an unconfirmed quote. Multi-item extraction uses the existing explicit table mapping, not this endpoint. Source-version replacement logic and retrieval from uploaded policy documents are not solved by this new entrypoint.

**Reference existence/read checks are not semantic entailment checks.** Even a wrong value with an existing reference remains an unconfirmed candidate, requiring human review. Do not describe schema/transport fixtures as extraction accuracy or prompt-injection immunity.

## API and migration

- POST `/api/v1/requests/{id}/candidate-sources` (multipart)
- POST/GET `/api/v1/requests/{id}/candidate-runs`
- GET `/api/v1/candidate-runs/{id}`
- POST `/api/v1/candidate-runs/{id}/process`, `/cancel`, `/import`

The new Alembic revision `f192c4ad8601` follows `e731bb62c905` and adds `quote_candidate_runs`. Upgrade before running private/PostgreSQL installations; back up existing data first. Downgrade removes candidate run history and requires deliberate operator review. Existing quote/approval/operation tables are not rewritten. Original immutable document files remain referenced by the existing backup machinery.

```bash
python -m pip install -r services/api/requirements-dev.txt
# From services/api with your existing local/private environment:
python -m alembic upgrade head
# Back at repository root:
python scripts/export_contracts.py --check
python scripts/test.py -q tests/test_quote_candidates.py
```

For local candidate model calls, set the flag and existing provider settings only in the controlled server process. Do not put provider credentials in client JS, Git, chat, Actions artifacts or screenshots. Existing demo Compose does not automatically inherit your host LLM credentials; this batch does not publicly deploy a paid-model endpoint.

## Reproducing Dev12

Use a new output directory for every attempt. No golden answer is used by the deterministic runner; grading reads gold separately. The grader has integrity/semantic limitations stated in `evals/dev12/README.md`.

```bash
RUN="evals/reports/local/dev12-$(date +%s)"
python scripts/evaluate_dev.py validate --output "$RUN/dataset.json"
python scripts/evaluate_dev.py predict-baseline --output "$RUN/observations.json"
python scripts/evaluate_dev.py grade --observations "$RUN/observations.json" --output "$RUN/scores.json"
```

`--require-all-success` makes any task failure a nonzero exit. Ordinary grade exit zero means the measurement completed, not that the application passed all tasks. Test counts, scripted responses, grader self-checks and historical CI are never substituted for real-model quality or final-commit acceptance.
