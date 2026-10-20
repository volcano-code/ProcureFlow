# Independent candidate Worker — implementation scope

Continue `9198004` without replacing existing pilot identity, policy, multi-item,
ERP, advice, or candidate features. This document describes behavior, not a claim
that remote CI or a real provider has passed. Read the exact-commit delivery report.

## Changed user path

1. Buyer uploads one TXT/text-PDF source and reserves a candidate run.
2. Explicit consent POSTs `/api/v1/candidate-runs/{id}/enqueue` and receives HTTP 202.
   The API does not construct a provider adapter or wait for its response.
3. A separate `python -m procureflow.candidate_worker` process polls the durable DB.
   It claims `QUEUED -> RUNNING` under the existing tenant/request/run locking order.
4. It reuses the bounded read-only candidate graph. The UI polls saved receipts.
5. A person may import the candidate as an **unconfirmed** quote. Confirmation,
   independent approval and the existing ERP operation Worker remain mandatory.

Legacy `/process` remains for compatibility and is marked deprecated in OpenAPI.
It cannot steal a queued task. It can still synchronously process a PENDING run
when explicitly used by an old client; the new native UI uses only `/enqueue`.

## Authorization and failure boundaries

The server stores tenant/user/role, pilot session ID and auth version, input hash,
mode and an integrity digest. **No bearer token, password or provider key** is stored
in the dispatch record. These are identity references, not a signed authorization
capability. Trust comes from rechecking live identity/session records and immutable
input binding; the DB itself remains a trusted component.

Pilot session expiry/revocation/role changes are checked at claim, between graph
steps and before accepting output. Static-token modes check the currently configured
actor, not possession of an old bearer token. API and Worker must use the same mode,
business DB, data directory and identity authority. Different input/policy/source or
maintenance generation invalidates the run. A worker has no ERP client/port.

At most five non-expired QUEUED/RUNNING tasks per workspace may be enqueued. Each
worker processes serially; multiple workers can claim different runs. This is not
a global cost reservation or distributed rate limiter. Existing per-run model/tool/
time/response budgets remain, and provider keys should have operator spending limits.

Cancellation before claim prevents a call. During a provider HTTP call, cancellation
cannot recall the request or its possible charge; it prevents further steps/acceptance.
RUNNING with an expired lease is marked INTERRUPTED, **never queued again**. A crash
just after claim may therefore lose work even before contacting a provider; choose a
new explicit user-authorized run, not a hidden retry. Provider pre-connect retries
inside the bounded existing adapter are distinct from replaying an uncertain run.

## Running (operator explicitly authorizes paid calls)

Stop old API/Workers and back up data. Apply `alembic upgrade head` from `services/api`
using the same database environment as the application. New migration `a40c72de1902`
follows `f192c4ad8601`; old candidate records keep null dispatch authority.

API environment: `PF_ALLOW_CANDIDATE_QUEUE=1`. The API does **not** require a model key
for this enqueue route. Existing synchronous explanation endpoints have their own
configuration; do not infer they are disabled by this change.

Separate Worker environment: same DB/data/mode and authority, plus
`PF_ALLOW_CANDIDATE_MODEL=1`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` provided privately.
From repository root:

```bash
PYTHONPATH=services/api python -m procureflow.candidate_worker --once
# Omit --once for a foreground long-running worker; default poll interval 3s.
```

Do not place provider credentials in browser/build args/Git/chat. Do not share demo
identities on a public endpoint. `compose.candidate-worker.yaml` is an opt-in overlay
for the existing local demo/mock stack and requires a private Worker env file. It is not auto-enabled; pilot/private deployments must supply an explicitly matched configuration instead of reusing this demo overlay.

## Reproducible validation (no real provider)

```bash
python scripts/test.py -q tests/test_candidate_queue.py tests/test_candidate_dispatch_migration.py
python scripts/verify_candidate_worker.py --fixture --output /tmp/new-worker-receipt.json
```

The second command starts a real local API, a separate Worker and a scripted HTTP
provider fixture, with fresh synthetic private identities and SQLite. It checks API
restart, no inline model calls, human import, cancelled queue, actual SIGKILL and
no replay. Lease expiry is explicitly injected, not measured as real elapsed time.
This is protocol/process acceptance, **not LLM quality, paid-model cost, PostgreSQL
process recovery or ERP joint acceptance**. UI polling tests use emulated receipts;
queue server tests separately run against SQLite and PostgreSQL via existing CI.

Downgrade requires stopping processes and explicitly cancelling/settling outstanding
work: migration refuses dropping the queue columns while any QUEUED/RUNNING rows exist.
No automatic destructive queue cleanup is performed. Existing frozen quote/approval
history and ERP records are unchanged.
