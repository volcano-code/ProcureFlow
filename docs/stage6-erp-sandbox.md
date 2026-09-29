# Disposable real ERPNext gate: initialization, permissions and evidence

This continues stage 5 on the existing FastAPI/Next.js/independent Python Worker stack. It does not migrate the application, access an existing ERP account, merge main, or submit a procurement document. Consult PR #1 and the Actions run bound to the exact commit for execution results; this document is not a passing test record.

## Repairs and diagnostics

The original run 36525414089 failed opening `/home/frappe/logs/database.log`. Frappe's logger uses paths relative to the bench `sites` directory; the standalone seed started from the bench root. Both seed and independent audit now use a shared fixed-site context manager that changes into `sites`, verifies the random lab marker before database connection, and always destroys the Frappe context and restores the caller's directory.

The first diagnostic implementation lacked `isatty()`, which Frappe uses during imports. It was replaced with a real UTF-8 null-device text stream supporting the text IO interface without buffering arbitrary output. Diagnostics export only a fixed stage, exception class and a bounded list of source-file basenames/line numbers. They do not export exception messages, frame locals, document bodies, absolute paths or credentials.

`bench --install-app erpnext` alone does not run the setup wizard's reference data initialization. The seed now calls ERPNext's official reference-fixture and default-value initializers before/after creating the synthetic company. It uses the installed leaf groups (`Local`, `Products`) and does not suppress link validation.

Frappe derives a standard user's type from the assigned role's desk access. The dedicated role is desk-enabled and the persisted integration identity must be `System User`; this does not grant the Administrator/System Manager/Buying Manager roles. The seed still asserts quotation read/create/write, denies submit/cancel/delete, and denies Purchase Order creation. Those checks alone do not prove that every internal validation query or every external operation will be authorized: the actual HTTP draft gate must also pass.

The loopback fault gateway exports, on failure, only the last 64 request observations: fixed endpoint family, method, status and an allowlisted error class. Where supplied by Frappe, bounded exception traces are reduced to allowlisted source filenames and line numbers. Mentioned DocType names are fixed categories, not proof of which permission was denied. Unknown exception text is replaced by a fixed failure reason. The CLI must return a nonzero exit code for every failed business report.

## Acceptance, not just startup

The pipeline must complete initialization, GET-only dedicated-identity preflight, approval safeguards, normal draft/readback/replay, lost-receipt recovery, API restart persistence, and an independent ERP database audit. A gateway injects a 504 only after the real ERP responds successfully; the next independent Worker must reconcile by reading, not repeat the POST. The required result is exactly two draft Supplier Quotations, one per operation key, and no Purchase Orders. This does not test an actual network outage or a process SIGKILL against ERP.

The independent database script checks remote IDs, operation keys, snapshot hashes, totals, draft counts/status and a real unique index. It attempts a duplicate-key update inside a savepoint, requires rejection and rolls that probe back. Detailed supplier/item/UOM/quantity/price/date checks belong to the application's independent readback, not every one to that database script.

The new evidence checker requires all four records (`seed.json`, `roundtrip.json`, `database-audit.json`, `image-digests.json`). Missing/failed stages, missing/repeated business steps, mock IDs, duplicate operations or drafts, unexpected write counts, unsafe permissions, an untested unique constraint, duplicate JSON keys or oversized input fail the check. The resulting `acceptance.json` hashes the source records. It is a consistency check, not a new execution or a cryptographic attestation; use the original Actions artifact metadata to bind it to the commit/run. A seed-only artifact cannot pass this gate.

## Safe local reproduction

Use a fresh disposable project and volumes only. Python 3.13 plus the pinned development requirements and Docker Compose are required. No existing company account is needed. The initializer refuses to overwrite its credential file; do not remove another lab's files or reuse its volumes just to force a run.

```bash
python scripts/init_erp_sandbox.py --create-ephemeral
export COMPOSE_PROJECT_NAME="pf-erp-local-$(date +%s)"
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml up -d
mkdir -p .data evals/reports/local/erp-sandbox
set -o pipefail
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml exec -T erp-backend /home/frappe/frappe-bench/env/bin/python /opt/pf-sandbox/seed.py | tee evals/reports/local/erp-sandbox/seed.json
# Continue only if the previous command succeeded.
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml cp erp-backend:/tmp/pf-erp-sandbox-credentials.json .data/erp-sandbox.json
chmod 600 .data/erp-sandbox.json
python scripts/verify_erp_sandbox.py --ephemeral-test --output evals/reports/local/erp-sandbox/roundtrip.json
# Continue only if the business gate succeeded.
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml cp evals/reports/local/erp-sandbox/roundtrip.json erp-backend:/tmp/pf-erp-result.json
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml exec -T erp-backend /home/frappe/frappe-bench/env/bin/python /opt/pf-sandbox/audit.py > evals/reports/local/erp-sandbox/database-audit.json
docker image inspect frappe/erpnext:v16.36.0 --format '{{json .RepoDigests}}' > evals/reports/local/erp-sandbox/image-digests.json
python scripts/check_erp_sandbox_evidence.py --directory evals/reports/local/erp-sandbox
# Destructive only to this uniquely named, explicitly disposable test project:
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml down -v --remove-orphans
```

Never commit `.env.erp-sandbox` or `.data/erp-sandbox.json`, copy credentials to a report, expose this lab publicly, or use these commands against existing company volumes. CI performs cleanup even after failure. Local operators must also clean up after a failed command.

## Scope retained

All business data and buyer/approver identities are synthetic; the role-based approvals are automated test actions, not a real human approval study. ERPNext and MariaDB are real software, but the ProcureFlow business database in this experiment is SQLite. Separate PostgreSQL/mock gates do not establish a joint PostgreSQL+ERP deployment result. There is no real model call, success-rate/cost benchmark, or 60-task evaluation. Mapping remains single-line explicit zero-tax/zero-freight/zero-discount draft quotations. Recorded ERP image digests do not pin every future image pull. Historical a2 version strings/MANIFEST and the older static aggregate OpenAPI export are not certification of this branch.
