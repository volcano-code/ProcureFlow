# Combined PostgreSQL + ERPNext acceptance

This increment preserves FastAPI, native Next.js and the independent Python Worker. It adds acceptance coverage, not a production ERP connector configuration or new procurement permissions.

## What the gate now requires

The isolated-real-erpnext-validation workflow runs a two-entry matrix: SQLite + ERPNext and PostgreSQL + ERPNext. Each entry gets a fresh isolated ERPNext/MariaDB installation. PostgreSQL business storage is explicitly selected with `--business-database postgresql`; it never falls back to SQLite. The separate mock-ERP PostgreSQL and Compose checks remain in the core workflow.

PostgreSQL requires `PF_ALLOW_DATABASE_TESTS=1` and `PF_TEST_DATABASE_URL` pointing to a loopback address and a dedicated database whose name ends in `_test`. URL query overrides and ambient `PG*` libpq variables are rejected before networking, so a service/host-address override cannot silently redirect the lab. The runner creates a random `pf_erp_test_*` schema, applies actual Alembic migrations, gives that same schema to the API and separate Worker processes, and removes only that schema on exit. It never reads the ordinary `PF_DATABASE_URL` as a target or changes database roles. The runner refuses optimized Python (`-O`/`PYTHONOPTIMIZE`) before networking; the new SQL audit uses explicit checks that cannot disappear with optimization.

Both matrix entries perform independent synthetic buyer/approver flows, reject unapproved/self-approved/stale writes, create and read back two real Supplier Quotation drafts, lose one committed HTTP receipt, recover by reading instead of recreating, reject cross-workspace verification, and preserve remote IDs across API restart. The PostgreSQL entry additionally checks its actual backend via `/ready` and reads the database independently after restart: current migration, two completed operations, two done outbox entries, matching remote identities and snapshots, and two persisted verification receipts.

## Negative REST permission evidence

After the successful roundtrip, `scripts/probe_erp_permissions.py` authenticates the restricted integration identity, reads the synthetic Company and both verified drafts, and checks exact-name Account selection. It then requires structured HTTP 403 `PermissionError` responses for seven actual REST requests:

- Account document read, write and delete
- Supplier Quotation submission and deletion
- Purchase Order list read and creation

Account selection is intentionally allowed: it supports ERPNext reference validation and is different from document read access. The existing Supplier Quotation draft read/create/write permissions are retained. The probe does not change permissions, repair unexpected success, or accept a bad request, missing record, failed login or generic HTTP error as authorization evidence. It stops on an unexpected result and keeps identifiers, credentials and response bodies out of reports.

Both draft contents are read again and must remain unchanged. The subsequent independent MariaDB audit still requires exactly two unsubmitted drafts, no Purchase Orders, active unique operation-key constraint, and the original restricted permissions. The aggregate checker requires all these records, all seven REST denials, select-only Account permission records, and the backend-specific business database evidence. Missing, skipped or inconsistent records fail the workflow.

These probes run only inside a fresh marked disposable lab. Never run them against production accounts or existing business volumes. They intentionally attempt forbidden operations on synthetic data; an unexpected permission grant makes the job fail and the entire disposable lab is removed.

## Aggregate API contracts

The committed `packages/contracts/openapi.json` now includes `/ready` and `POST /api/v1/operations/{operation_id}/verify`. Existing routes/components and the standalone request/quotation schemas are preserved. `python scripts/export_contracts.py` regenerates all three contracts in a fresh subprocess and temporary mock/demo database. `python scripts/export_contracts.py --check` compares exact bytes without overwriting files; the core workflow requires this check. Ambient ERP credentials and application database configuration are not inherited by generation.

## Running and interpreting results

Install both `requirements-dev.txt` and `requirements-postgres.txt`, provision the disposable ERP lab as in `.github/workflows/erp-sandbox.yml`, copy only its private credentials to `.data/erp-sandbox.json`, and provide a separate local test PostgreSQL database. Then run:

```bash
python scripts/verify_erp_sandbox.py --ephemeral-test --business-database postgresql --output evals/reports/local/erp/roundtrip.json
python scripts/probe_erp_permissions.py --ephemeral-test --roundtrip evals/reports/local/erp/roundtrip.json --output evals/reports/local/erp/permission-probes.json
```

Run the container-side independent audit and record image identities exactly as the workflow does before invoking the aggregate checker with the matching `--business-database`. One backend's records cannot certify the other backend.

Baseline commit `31065d03dd034e1f0745a66e5b36ea411cb86b8a` passed its earlier SQLite + real-ERP workflow [36552704080](https://github.com/volcano-code/ProcureFlow/actions/runs/36552704080). That historical run does not certify these additions. Current acceptance must come from completed workflows for the exact new commit, with both matrix entries and their uploaded records. Unit fixtures test report validation only. This is not production certification, a live-model benchmark, a real human approval study, or an authorization to submit purchase orders.
