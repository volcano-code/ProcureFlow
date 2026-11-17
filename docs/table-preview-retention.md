# Reversible table-preview retention

This local operator workflow archives only expired, unimported table previews. It
never deletes or moves source files, purges database rows, or changes procurement
requests, imported documents, quotes, quote versions, provenance, approvals,
evaluations, advisory receipts, ERP operations, outbox entries, or existing audit
history. It is not a disk-space reclamation mechanism. Source bytes remain subject
to the deployment's storage capacity and backup policy.

## Eligibility and quota

- A new upload has a 30-minute mapping-preview lifetime. A tenant may have at most
  100 active `OPEN` previews. Expired, unimported, unreferenced previews and
  `ARCHIVED` previews no longer consume that active quota.
- Re-uploading an expired `OPEN` source uses the same quota check as a new upload,
  clears the old mapping, advances its revision, and requires a new reviewed
  preview before confirmation. An archived source stays archived on duplicate
  upload; it requires an explicit operator unarchive first.
- Archive eligibility requires `OPEN`, a valid timezone-aware expiry, no quote,
  and no document/reference ownership. Defensive checks protect the derived
  document ID, same-request source digest, and globally shared storage keys,
  including another imported preview's key. Malformed timestamps and protected
  `OPEN` rows count against quota and are excluded from archive plans.
- The default retention grace is 24 hours **after expiry**. Operators may select
  1–8,760 hours. Each plan contains at most 100 rows by default, configurable to
  1–1,000. Run another reviewed plan for the next batch. Imported history and
  archived bytes have no automatic expiry or purge.

## Operator commands

Use the same `PF_DATABASE_URL`, `PF_DATA_DIR`, and `PF_MODE` as the intended local
installation. Restrict database and plan-file access to authorized operators.
`--actor` is audit attribution, not authentication or a grant of business role.
No command connects to an ERP. No HTTP retention endpoint or scheduled cleanup is
installed. PostgreSQL and private/pilot installations must already have current migrations;
the CLI never creates or migrates schemas.

From the repository root with API dependencies installed:

```sh
export PYTHONPATH=services/api
python -m procureflow.retention_cli plan --tenant demo --output archive-plan.json
```

`plan` is a dry run. It reads candidate state without changing rows or source
files. `--output` creates a new owner-only file and refuses to overwrite an
existing path. Omit `--output` to print the plan JSON. Review its tenant, operation,
IDs, expiry and revisions before applying. Avoid placing plans in shared logs.

```sh
python -m procureflow.retention_cli apply --tenant demo --actor operator-01 \
  --plan archive-plan.json --apply
```

Without `--apply`, nothing is mutated. Each successful transition changes only
preview status and revision and appends an attributable audit event with the
plan ID and before/after fingerprints. All changes in the batch commit together;
a stale/ineligible member rolls the entire batch back.

Plans expire after 60 minutes. Any edited plan, reopened/confirmed preview,
changed mapping or source reference is rejected. Create and review a fresh plan
instead of editing an old plan to force an action. A plan fingerprint detects
accidental edits; it is not a signature or authorization token.

After a lost response or process restart, replay the same still-valid plan.
Matching database audit receipts make this an idempotent no-op. A subsequent
unarchive, reopen or other change makes the old plan stale. An expired plan is
rejected even on retry; use a fresh plan to inspect the current state.

## Undo without reviving confirmation permission

```sh
python -m procureflow.retention_cli plan --tenant demo --operation unarchive \
  --import-id tim_REPLACE_WITH_32_HEX_ID --output unarchive-plan.json
python -m procureflow.retention_cli apply --tenant demo --actor operator-01 \
  --plan unarchive-plan.json --apply
```

Unarchive requires explicit preview IDs (`--import-id` can be repeated). It keeps
all source data, mapping, provenance and the **original expired timestamp**, and
advances the revision again. It does not make the source confirmable. A buyer must
explicitly upload the source again, review a fresh mapping revision, and confirm
through the normal request/role/version checks. No approval or frozen request is
revived by retention.

## Concurrency and recovery boundary

SQLite uses the existing `BEGIN IMMEDIATE` write serialization. PostgreSQL uses
the existing tenant anchor, followed by request and preview locks, in the same
order as upload and confirmation. Eligibility and fingerprints are rechecked
inside the mutation transaction, including expiry of the plan after lock waits.
An archive and a re-upload either serialize to an archived readback or reject the
stale plan. Confirmation of an expired or archived preview cannot create a quote.

There are no retention filesystem transitions to crash between: source files stay
in place, and row status plus audit receipt are a single database transaction.
Database/document backup and isolated restore verification remain separate
operator workflows. There is intentionally no purge command.
