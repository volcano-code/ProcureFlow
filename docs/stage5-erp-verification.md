# ERP draft verification and recovery hardening

> Historical scope: this document describes the earlier ERP gate. The current four-draft tax/freight/discount contract and its verification limits are in [ERP cost mapping](erp-cost-mapping.md). Earlier passing results do not validate the new mapping.

## User-facing change

The native Next.js workbench now offers an independent ERP read-back action on an existing operation. `POST /api/v1/operations/{id}/verify` writes a local audit event but performs only ERP reads. It never creates, retries, submits or deletes an ERP document and never changes the historical operation status.

A receipt contains the observation time, original snapshot hash, simulated/live distinction and one of `verified`, `mismatch`, `missing`, `unavailable`, or `blocked`. A historical COMPLETED operation is not a claim that the ERP document still matches today. Missing data is not proof that the earlier write never committed.

The verifier checks the operation key, document identity, draft status, supplier, item, UOM, quantity, unit price, total, currency, company, transaction date and snapshot hash. Corrupt stored snapshots or changed ERP targets block network access. Recovery uses the same checks; this fixes missing price/date/key checks in the former recovery path.

All workspace readers, including auditors, can request a receipt. Unauthenticated and cross-workspace requests remain rejected. The model still has no approval or external-write authority. Legacy mock drafts lacking price/date are not silently enriched or certified; retain their historical records and use a new synthetic request for a complete new receipt.

## Disposable real-software ERP lab: historical state at fc41b86

`compose.erp-sandbox.yaml` creates a separate ERPNext/MariaDB test installation, fixed to `pf-erp-test.local`, with random local credentials and a matching random lab marker. It does not use the user's company account. The initializer requires `--create-ephemeral`, refuses to overwrite credentials, and writes a mode-0600 file. The roundtrip runner requires `--ephemeral-test` and refuses mismatched markers, company, site, user or item before networking.

The intended gate provisions synthetic master data plus an integration role without submit/cancel/delete/Purchase Order creation permissions. It then tests normal draft creation and a gateway-injected 504 after the real ERP commits. It must independently read the ERP database, prove uniqueness, preserve draft status and retain the same IDs after API restart. Business state in this dedicated experiment is SQLite; ERP state is MariaDB. This is not a PostgreSQL+ERP joint test, a real human approval study, a live-model benchmark, or production certification.

The first actual run, 36525414089 at commit 00ad3c7, FAILED in `Seed synthetic master data and restricted identity`, after the ERP startup step succeeded. Business roundtrip and independent database assertions did not execute. At that delivery the exact exception had not been reliably extracted. Stage 6 subsequently identified the relative logging-path failure and added repairs plus bounded diagnostics; see [stage 6](stage6-erp-sandbox.md) and the exact-commit CI results for the current state. Do not treat container startup, this configuration, or the existing mock tests as successful real-ERP acceptance.

Run instructions for a fresh isolated lab (development diagnostics only until this gate is repaired):

```bash
python scripts/init_erp_sandbox.py --create-ephemeral
export COMPOSE_PROJECT_NAME=pf-erp-local-test
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml up -d
docker compose --env-file .env.erp-sandbox -f compose.erp-sandbox.yaml exec -T erp-backend /home/frappe/frappe-bench/env/bin/python /opt/pf-sandbox/seed.py
```

Only after successful initialization should the private credential file be copied into `.data/erp-sandbox.json` and the explicit roundtrip gate run. See `.github/workflows/erp-sandbox.yml`. Never reuse existing company volumes, commit/share `.env.erp-sandbox`, expose demo identities publicly, or force a write to bypass approval. CI destroys only its uniquely named disposable project.

## Regression evidence and remaining work

Local combined non-browser/non-PostgreSQL suite: 210 passed, 11 deselected. This includes 17 new ERP verification/recovery cases and nine sandbox authorization/file-safety cases. The native E2E success case now exercises the read-back button and asserts that the historical status remains unchanged. Browser/PG/container results must be checked on the final commit; earlier 215bc8f results do not certify this change.

Historical note: at this stage, the checked-in aggregate OpenAPI export predated this endpoint. [Stage 7](stage7-combined-acceptance.md) regenerates the aggregate and adds a non-mutating CI drift gate. The REST server also exposes its current OpenAPI dynamically. No business-table migration is introduced in this iteration. Main is unchanged; no automatic merge, release or production ordering is requested.

Next engineering blocker: obtain a bounded, sanitized initialization exception from the disposable ERP job, fix the fixture provisioning and pass normal/lost-receipt/database-audit checks. Real provider credentials and user-account integration remain separate, opt-in work.
