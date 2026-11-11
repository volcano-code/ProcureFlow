# ProcureFlow read-only AG-UI 1.0 observation

## Scope and endpoint

`POST /api/v1/ag-ui/advice-runs/{run_id}/observe?after=<audit_id>` is a **read-only observation of an already-persisted AdviceRun**, not the usual AG-UI agent invocation. It returns a finite, official `ag-ui-protocol==1.0.0` SSE event stream with `RUN_STARTED`, `STATE_SNAPSHOT`, bounded allowlisted `CUSTOM` progress events, optional assistant `TEXT_MESSAGE_*`, and `RUN_FINISHED`. The last event **ends the observation request**, **not** the provider run. `STATE_SNAPSHOT.agentStatus` remains the authoritative durable status (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `INTERRUPTED`, `STALE`). An observer connection never calls `/process`, `/approve`, `/execute`, any ERP endpoint or any LLM. `RUN_FINISHED` contains `providerSuccessNotImplied=true` to prevent conflating the two lifecycles.

The request accepts only this AG-UI `RunAgentInput` subset (extra keys are rejected):

```json
{"threadId":"req_...","runId":"adv_...","protocolVersion":"1.0","messages":[],"tools":[],"context":[],"state":{}}
```

`runId` must match the URL and `threadId` must be the saved run's request. No new messages, tools, resume entries, mutable forwarded props, arbitrary URLs or role/tenant overrides. Pass an authenticated current `Authorization: Bearer ...` header. Reconnection uses `after` set to the last validated `STATE_SNAPSHOT.snapshot.auditCursor`; it never resends the process call. This is a short, finite observation so it does not rely on an EventSource connection which cannot attach a private bearer header in all browsers. The browser polls only an existing receipt, with a single active observer per selected request and source version.

The server reads the current durable advice receipt, tenant-scoped audit events and advice receipt again. Cross-tenant access, revocation, input/document version changes and source-integrity failures retain the original business denial semantics; a concurrent run status change returns `409 AGUI_SNAPSHOT_CHANGED` instead of a mixed snapshot. The projection has an allowlist of audit kinds and scalar metadata; source text, secrets, private reasoning, raw model messages, and tool arguments are **never** forwarded from audit logs. A completed, current advice summary can appear as ordinary `TEXT_MESSAGE_*`; the UI renders text safely and does not claim semantic truth. All data remain subject to normal authentication and evidence expiry.

Human interaction is intentionally **not an AG-UI model tool**. `STATE_SNAPSHOT.humanAction` distinguishes manual review, explicit buyer dispatch, read-only observation and human reconciliation on unknown outcomes. An observer cannot resume, cancel, approve or execute work. Existing procurement forms continue to require separate server-side authority and frozen snapshot checks.

## Wire contract & tested boundaries

Python SDK `EventEncoder` is used to encode the actual protocol events (not hand-written JSON events renamed as AG-UI). Cursor IDs are attached only to durable `CUSTOM` progress events. After a stream is interrupted, clients replay from their last valid integer audit cursor; `STATE_SNAPSHOT` always gives a fresh source-bound status and maximum scanned cursor. The JS decoder enforces framing, lifecycle order, run/thread identity, limits, explicit finished marker and cursor integrity. Unfinished or invalid streams cannot be treated as completed. Source-fenced cleanup cancels the local observer, **not** a provider request; a disconnected observer does not guarantee the provider stopped or did not bill.

Use the official SDK and a disposable real API/database test:

```bash
python -m pip install -r services/api/requirements-dev.txt -r services/api/requirements-agui.txt
PYTHONPATH=services/api python -m pytest -q tests/test_agui_observer.py integrations/agui/tests/test_http_protocol.py
npm ci --prefix apps/web
npm run test:unit --prefix apps/web
npm run typecheck --prefix apps/web
npm run build --prefix apps/web
```

`services/api/tests/test_agui_postgres.py` is run separately against a dedicated `*_test` PostgreSQL database (`PF_ALLOW_DATABASE_TESTS=1`), with an isolated schema, real Alembic upgrade and independent business-side-effect count check. Never run it against a production database. GitHub Actions `.github/workflows/agui-observer.yml` runs both. No real LLM/ERP writes or credentials are required.

### Deliberate limitations

This is not a general **AG-UI agent-execution server** for arbitrary clients or a WebSocket transport; clients cannot submit a conversation, tools or tool results to initiate a model run. Existing LangGraph/AdviceService owns execution. `CUSTOM` progress events are intentional: the durable audit has safe metadata but not the full model tool arguments/results required to truthfully reconstruct all canonical tool events. Human intervention is **displayed**, not resumed through AG-UI. There is no cross-process push broker, guaranteed real-time latency or event-store retention SLA; polling may miss events pruned by retention, so every observation begins with a fresh database state snapshot. There is no live-model task-success/cost measurement, reviewed semantic citations or external-user study.

Deployment installs pinned `ag-ui-protocol==1.0.0`; no model API key is needed just to observe. Do not show a credential in the URL, source code, terminal logs, issue, browser localStorage or CI artifact.
