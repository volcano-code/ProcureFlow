# Stage 15: paired backup and isolated recovery

This is an **operator-only, fail-closed local/pilot recovery path**, not a
production disaster-recovery certification. Do not use it to experiment on real
pilot data. Run the synthetic tests first. PostgreSQL parity is an opt-in live
integration gate; a skipped PostgreSQL test is not a passing gate.

## What is preserved

A version-1 `.pfb` file pairs a typed logical database snapshot with every raw
file referenced by a document or retained table-import preview. It preserves
requests, source fragments, quotes and versions, confirmations, policy versions,
approvals, evaluations, advisory receipts, audit events, external-operation
idempotency keys, payloads, remote IDs, attempts, leases, errors and outbox rows.
Unreferenced files and environment/configuration files are excluded. No existing
source evidence is deleted by backup or recovery. Preview retention is a separate
operation and never authorizes deleting confirmed evidence.

The source must have the exact current Alembic head, known table/column/types,
keys and indexes. Merely calling the demo `create_schema` helper is insufficient.
For a new deployment, run Alembic before the first application startup. An older
`create_schema`-only demo database needs a separately reviewed migration-adoption
procedure; do not blindly stamp it or run initial migrations over existing tables.
That adoption is outside this increment. The source must be explicitly paused. `Database.maintenance()` then holds the
cross-process exclusive activity fence and a serializing transaction throughout
row export and raw-file copying. SQLite uses the adjacent activity lock and
`BEGIN IMMEDIATE`; PostgreSQL uses the schema-scoped advisory fence. Missing,
symlinked, changing or hash-mismatched referenced files abort the backup.

Before relying on the fence, stop all older binaries that do not implement it,
and stop direct SQL/file writers, imports, migrations and external maintenance
scripts. All API, worker and offline writers must use the same SQLite database or
PostgreSQL schema. SQLite symlink aliases resolve to the canonical database path;
hard-linked databases are actively rejected. PostgreSQL resolves the
`system_state` relation to identify its fence. Do not alias source/target data or
document directories with symlinks or hard links. The fence does not lock an ERP service;
ERP outcomes must still be reconciled after a restore.

## Backup and verify

Run from `services/api` with the supported Python environment. Choose a fresh
output file outside the document directory. The output is owner-only and is never
overwritten. A source revision may be a 7–64 character lowercase Git SHA or the
explicit default `unversioned`; the executable-source fingerprint is always stored.

Prepare an owner-only file containing the full source database URL. Create it
through your normal secret-management workflow, with mode `0600`; do not put
password-bearing URLs in process arguments, shell history or logs. The CLI refuses
symlinks and group/world-readable URL files. No URL file is included in backups.
The maintenance CLI uses the current operator's explicit `PF_DATABASE_URL` and
normal application settings; check those settings before pausing.

```sh
python -m procureflow.maintenance_cli pause --apply
python -m procureflow.backup_cli backup \
  --database-url-file /secure/source-database-url \
  --documents /srv/procureflow/documents \
  --output /secure/backups/recovery-point.pfb \
  --source-revision <git-sha>
python -m procureflow.backup_cli verify /secure/backups/recovery-point.pfb
```

The source remains paused after both success and failure. Review its current
ledger before explicitly resuming it; use the `generation` and `ledger_sha256`
returned by a fresh report, not a stale copy:

```sh
python -m procureflow.maintenance_cli report
python -m procureflow.maintenance_cli resume --apply \
  --generation <reported-generation> --ledger-sha256 <reported-sha256>
```

## Strict bundle contract and limits

The archive contains exactly `manifest.json`, `database.json`, and the referenced
`documents/<storage-key>` entries. The manifest records a random backup ID, UTC
creation time, source backend/Git revision/executable-source fingerprint, complete
logical schema, Alembic heads, row counts, byte counts, SHA-256 digests and fixed
version-1 limits. Unknown members, tables, columns, versions or schema changes
fail closed. SQL supplied by an archive is never executed. Schema creation and
inserts use only installed, trusted migrations/models and bound values.

Limits are intentionally small and non-configurable by the archive:

- 256 MiB total archive, 64 MiB database JSON, 2 MiB manifest
- 16 MiB per document, 10,002 members including the two JSON files
- 100,000 rows per table, 500,000 rows total, JSON nesting depth at most 64
- 1,000,000 JSON structural/string tokens before parser allocation
- Classic single-disk, uncompressed ZIP only; no ZIP64, encryption, comments,
  extra fields, data descriptors, prepended/trailing data or overlapping records
- Flat ASCII storage keys, no absolute paths, nested paths, traversal, duplicate
  names/JSON keys, symlinks or special files

A bounded, streaming central-directory preflight runs before the ZIP library
allocates member objects. It validates both declared and actual entry counts and
local-header ranges. Compressed archives are rejected rather than decompressed.
All JSON types, nullability, lengths, keys, relational/unique constraints,
checksums and document references are validated using an isolated in-memory
schema before any restore directory, target database connection or PostgreSQL schema is
created. The source executable fingerprint and full schema must match the
installed recovery code exactly. The stage-16 diagnostic module changes the
executable fingerprint: a baseline `a10f62b` backup still requires matching
trusted baseline recovery code. No backward-compatible backup migration is
introduced by that increment. Do not manually relax version checks to import
an older release; use the matching trusted release and a separately reviewed
upgrade procedure.

The bundle contains sensitive business data and credential hashes. It is **not
encrypted or cryptographically signed**. SHA-256 detects corruption and pairing
mistakes; it does not authenticate a maliciously rewritten manifest. Store and
transfer bundles using your approved encrypted, access-controlled process. Never
publish them or attach them to an issue. Possession of a structurally valid bundle
does not establish the truth of its business records.

## Restore only into a fresh isolated target

Restore fully verifies the bundle first. The destination directory must not exist,
its parent must already exist, and an existing file, directory or symlink is
rejected. There is no overwrite, merge, in-place restore, `pg_restore`, purge or
force option. A failure retains its new resources and `.restore-incomplete`
marker for explicit operator investigation; retry to another fresh target. Do not
remove that marker to make a partial restore boot. No automatic directory removal
or schema drop is performed.

SQLite creates only `<new-dir>/procureflow.sqlite3`, the paired `documents/`
directory and a credential-free `recovery-report.json`:

```sh
python -m procureflow.backup_cli restore /secure/backups/recovery-point.pfb \
  --data-dir /srv/recovery/drill-001
```

For PostgreSQL, supply an explicit owner-only URL file for an already provisioned
recovery database. Prefer a dedicated isolated recovery database and restricted
operator account. The command **always generates a new random
`pf_restore_<uuid>` schema** and overrides inherited `search_path` options. It
never restores into a schema name from the archive, an existing schema, the
source schema or `public`. The returned report contains the new schema name but
no URL/password. Configure a separate application's PostgreSQL URL to use exactly
that schema and its paired fresh data directory; do not use an ambient/default
schema. The CLI does not read `PF_DATABASE_URL` for restore.

```sh
python -m procureflow.backup_cli restore /secure/backups/recovery-point.pfb \
  --data-dir /srv/recovery/drill-pg-001 \
  --postgres-url-file /secure/recovery-database-url
```

Both backends install the recovery fence within the same transaction as schema
creation. API/worker startup also refuses `.restore-incomplete`. That marker is
removed only after successful import, integrity verification and report creation.
A `BACKUP_RESTORED` audit receipt binds the restore to the original manifest and
database hashes while preserving all original audit rows. Local files are
owner-only; no copied `.env`, ERP/model secret, token configuration or credential
file is created.

## Restored authority and ERP uncertainty

Successful restores always enter durable `RECOVERY`, require `PF_MODE=pilot`, and
block ordinary writes, session creation, invitation consumption and worker
execution. All restored sessions and invitations are revoked. Prior revocation
timestamps remain intact. Tenant/member generations increase, invalidating old
approval/initiator authority. Global recovery generation increases too. Configure
fresh authorized secrets through the existing operator workflow; old private/demo
tokens cannot reactivate a restored database.

Every non-`COMPLETED` operation receives a permanent recovery hold. Existing holds
are retained. Operation/outbox statuses, idempotency keys, payloads, remote IDs,
leases and attempts remain evidence and are not reset to "pending". This includes
`PENDING`, `IN_FLIGHT`, `RECONCILING`, failed and manual-review work. **Nothing is
automatically replayed**, even after a later explicit resume.

1. Keep API/worker outbound connectivity disabled during the drill
2. Inspect the credential-free offline ledger with `maintenance_cli report`
3. Compare each uncertain operation with authoritative ERP records through an
   approved read-only process, using its original idempotency key and remote ID
4. Record the recovery decision and complete the credential review. A missing ERP
   response is not evidence that the ERP did not commit
5. Only then explicitly acknowledge the exact fresh report to permit new pilot
   work. A changed generation or ledger digest rejects the command

```sh
python -m procureflow.maintenance_cli resume --apply \
  --generation <reported-generation> --ledger-sha256 <reported-sha256> \
  --restore-id <reported-restore-id> \
  --acknowledge-reconciliation --acknowledge-credentials
```

Resume permits newly authorized work only. Restored-operation holds stay in
place; this increment intentionally provides no release/replay command. Use the
existing offline pilot administration workflow to reissue controlled invitations
after the review. With reviewed, fresh pilot authority, held operations remain
accessible through the GET-only ERP verification path. The
[stage 16 diagnostics](stage16-recovery-diagnostics.md) add a held-operation
queue, evidence detail and explicit comparison results under already-valid
authority; they do not enable login while `RECOVERY` is active or replace the
offline review above. The
synthetic `verify_recovery.py` acceptance drill checks a missing ERP receipt,
zero external writes and an unchanged held ledger. Never treat this procedure as
an ERP reconciliation engine or permission to create replacement purchase orders.

## Synthetic verification

```sh
PYTHONPATH=services/api pytest -q services/api/tests/test_backup_recovery.py
```

The suite uses disposable synthetic records and files. It covers paired SQLite
roundtrip and evidence equality, credential invalidation, permanent holds,
explicit resume, failed-restore markers, existing-target protection, source
schema/file drift, corrupt manifests, malicious-but-rehashed rows, hostile ZIP
names/types, bounded central-directory preflight, archive/resource limits,
configuration exclusion and credential-safe CLI output.

The PostgreSQL test uses the existing `pg_database` fixture. It requires
`PF_ALLOW_DATABASE_TESTS=1`, an explicit `PF_TEST_DATABASE_URL` whose database ends
in `_test`, and the PostgreSQL requirements. It creates a new random restore
schema, checks preserved ledger values, recovery state and audit sequence
advancement, and drops only that synthetic test schema in fixture cleanup. It
never falls back to SQLite. Run `scripts/verify_postgres.py` in the separately
provisioned CI environment for a required live gate. Local skips must remain
visible in the verification record. The original local stage-15 implementation
environment had no disposable
PostgreSQL server, so its local SQLite results did not establish PostgreSQL
parity. The subsequently accepted `a10f62b` package records a successful live
PostgreSQL gate for that exact baseline; see the
[baseline evidence index](evidence/a10f62b-baseline.json). Those historical
results do not validate
later changes or replace a current-increment live gate.
