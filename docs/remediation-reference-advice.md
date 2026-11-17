# Version-bound references in Advice, MCP and the native workbench

Base: d36d520. This is the next feature after the versioned document library.
No production deployment, purchase, paid LLM call, new database migration or permission expansion is required by this change.

## What changes

The existing `search_policy` still reads executable structured rules. Two additional read tools are shared between Function Calling and the official MCP stdio server:

- `search_references(query, limit=5)` searches advisory text in one run's bound procurement corpus. It cannot accept tenant, URL, scope, role, model or retrieval-method overrides.
- `get_reference_fragment(citation_id)` reads exactly one fragment in that same currently valid corpus. Other revisions, tenants and nonexistent fragments are rejected.

The advice input snapshot now includes `reference_library`: active revision IDs, versions, source/revision hashes, corpus fingerprint, retrieval method and encoder identity. This adds metadata, not all reference text or vectors, to the saved snapshot. Empty corpora are bound too. Newly published active documents, replacements, withdrawals, expiry and scheduled activation invalidate the old run. Future versions do not invalidate it before activation. Legacy advice without this binding stays historical/noncurrent; it cannot silently acquire current references. No stored history is rewritten.

Retrieval is outside long-held SQL transactions. Before and after retrieval the service rechecks current authority, quotation/request/rule inputs and the active reference corpus. Missing/incompatible embeddings fail closed; there is no silent mode switch. In the app, the library UI and AdviceService use the same configured encoder. Exact, bounded local vector search remains the implementation; this change does not add pgvector or ANN.

Model results still use `summary` and `evidence_ids`. Only references actually returned by the trusted read path can join the allowed IDs; the graph also requires cited fragments to have been read. Quotation citations remain necessary when quotation sources exist: a reference citation cannot substitute for evidence of a price. `reference_citations`, `reference_retrievals` and `reference_binding` are attached by the server, never accepted from model output. Semantic entailment is not automatically certified.

## HTTP and MCP

Authenticated, read-only additions to the existing mounted reference API:

```
GET /api/v1/policy-library/advice-runs/{run_id}/search?query=...&limit=5
GET /api/v1/policy-library/advice-runs/{run_id}/fragment?citation_id=...
```

The core OpenAPI stays unchanged; regenerate/check `packages/contracts/policy-library.openapi.json` with `python scripts/export_policy_contract.py [--check]`.

The MCP bridge still binds one operator-configured advice receipt. It validates every reference response against the input hash, request/run IDs, canonical argument hash, corpus, encoder, revision metadata and snippet hash. It re-reads the receipt after reference retrieval, so revoked/stale sessions are denied before disclosure. Paths are fixed; user input becomes validated query parameters only. The existing total 8-second deadline, one-request concurrency, 64-call process budget and response byte limit remain; no retries/redirects or cached authorization. Additional reads do not extend those budgets. MCP HTTP/OAuth is not provided.

## Native UI

Advice history displays reference titles/versions and a distinct exact-readback button. The click uses the authenticated bound endpoint, compares the response to saved metadata and opens the original fragment. A stale result remains visible as history; its reference button is disabled after freshness refresh. Readback errors trigger a status refresh. Refreshing/clicking cannot start a model call. Reference text is rendered as plain text, not HTML or executable instructions. Publication does not approve a quote or alter the executable policy.

## Verification commands (disposable environments)

```
python -m pip install -r services/api/requirements-dev.txt -r services/api/requirements-mcp.txt
python -m pytest -q tests/test_advice_references.py tests/test_mcp_bridge.py integrations/reference_advice/tests integrations/mcp/tests
python scripts/test.py -q tests/test_advice_runs.py tests/test_advice_grounding.py tests/test_graph_runtime.py tests/test_adapters.py
python scripts/export_contracts.py --check
python scripts/export_policy_contract.py --check
npm ci --prefix apps/web
python scripts/verify_next.py --reference-advice --output /tmp/pf-reference-advice-browser
```

The dedicated `version-bound-reference-advice` workflow additionally exercises migrated PostgreSQL and a separately provisioned real offline BGE encoder inside a scripted advice loop. The graph protocol/model response is a fixture, not real LLM intelligence. The native browser test reads an actual service-generated fixture receipt from its isolated SQLite database; it does not intercept business HTTP and does not represent live-model generation. The PostgreSQL, stdio, browser and neural tests are distinct experiments, not a single combined experiment. Model weights and credentials are not part of source packages.

## Remaining boundaries

Actual LLM output quality, human semantic review, independent evaluation and user trials still require real execution. AG-UI, remote MCP authorization, large-scale retrieval and reviewed release remain separate work. Engineering test counts must not be reported as task quality or combined across overlapping runs. Old CI results cannot validate a newer source tree.
