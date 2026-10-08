# Stage 16: read-only recovery diagnostics

This is an **unreleased local development increment** from accepted source commit
`a10f62b65263c9e6bb53219a6296dfb9a67de527`, tree
`90ec220e6c35a28add728d47e6cfa851f591808e`. Local regression, frontend units and
build checks passed; native browser execution is blocked by the environment, and
current-increment live integration gates were not run. The accepted baseline's
CI is historical evidence, not a passing result for this increment. No publication, merge or deployment is part of
this work, and independent security review remains incomplete.

## Purpose and authority boundary

A held operation needs a reviewable trail: which restore held it, what was
approved, whether its source bytes still match, and how a read-only ERP response
differs from its persisted snapshot. These observations help an operator inspect
uncertainty. They never authorize another purchase or an ERP write.

[Stage 15](stage15-recovery.md) remains the restore and maintenance contract:

- Restore targets a fresh isolated location, enters `RECOVERY`, requires
  `PF_MODE=pilot`, and revokes old sessions and invitations.
- Ordinary writes, new login/session creation, invitation consumption and worker
  execution remain blocked while `RECOVERY` is active.
- Every restored non-completed operation retains its permanent recovery hold.
  Status, attempts, leases, original snapshot, idempotency key and outbox evidence
  are preserved, including after an explicitly reviewed resume.
- There is no HTTP resume route, hold-release/replay control, reconciliation
  acknowledgement endpoint or new ERP write authorization in this increment.
  No verification result releases a hold or revives an approval.
- There is no automatic polling, retry-to-create, replacement order or automatic
  reconciliation decision. A missing ERP record is not proof of no prior commit.

The API and native UI require already-valid authority under the existing role,
tenant, membership, expiry and revocation checks. They do not provide a login
bypass for a freshly restored system. Start with offline review. Use the UI only
after the existing operator process permits newly authorized pilot access.

## Offline review comes first

Keep outbound connectivity disabled during a restore drill. Use the existing
maintenance inventory for the fresh generation and whole-ledger digest:

```sh
PYTHONPATH=services/api python -m procureflow.maintenance_cli report
```

For the local source, snapshot and approval evidence of a specific held operation:

```sh
PYTHONPATH=services/api python -m procureflow.maintenance_cli inspect \
  --tenant-id <tenant-id> --operation-id <held-operation-id>
```

These commands use the intended installation's `PF_DATABASE_URL`, `PF_DATA_DIR`
and `PF_MODE`; use the same protected configuration and operator access as stage
15. The `inspect` command does not contact ERP, create a user/session, resume
writes or update the ledger. It requires the tenant and operation explicitly.
Offline approval evidence reports that current principal authority was not
evaluated; possession of the report is not business authorization.

Complete the original reconciliation and credential review before the explicit
stage-15 `resume --apply` workflow. Only a fresh maintenance `report` supplies the
`generation`, global `ledger_sha256` and `restore_id` for that command. A
per-operation diagnostic digest is not interchangeable with that global digest.
Resume still permits only newly authorized work. Old operations remain held.

## Authenticated API

All three routes use `GET` and the existing authenticated tenant scope:

| Route | Result | ERP access |
| --- | --- | --- |
| `/api/v1/recovery/operations` | Held-operation queue, recovery state and pagination | None |
| `/api/v1/recovery/operations/{operation_id}` | Local approval, snapshot and source-integrity details | None |
| `/api/v1/recovery/operations/{operation_id}/reconciliation` | Explicit read-only comparison with the original ERP target | Read-only lookup |

The list defaults to 50 rows, accepts `limit` from 1 to 100, and uses the returned
`next_after` value as the next `after` cursor. It lists holds, not every failed
operation. A held operation outside the caller's tenant is not disclosed. None
of these reads changes operation/outbox state, approval, source files, recovery
holds or the maintenance generation. The comparison route does not append a
local audit receipt; its result is an observation returned to the caller.

### Local detail

Details include the original hold and status, attempts, remote ID if known,
snapshot and payload digests, a per-operation ledger digest, and allowlisted
expected ERP fields. Approval evidence distinguishes status, expiry, snapshot
match and whether current approval authority was evaluated. It does not treat a
historical approved row as fresh permission.

Sources are bound back to the persisted quote versions and original document
hashes. Each source has an integrity result (`verified`, `missing`, `mismatch` or
`unavailable`). A missing or unsafe file is reported rather than silently replaced.
The local detail does not run OCR, repair source bytes or change quote evidence.

### ERP comparison

The lookup uses the original operation key. A changed execution target or corrupt
snapshot blocks the lookup. The comparison includes the persisted remote ID when
known, draft state, supplier, SKU, quantity, unit price, UOM, currency, total,
company, transaction date, snapshot hash and the supported cost contract. Cost
components are checked as well as the total, so equal totals alone do not establish
a match. Decimal quantities and amounts follow the existing exact-value contract.

The result contains `expected`, `observed` and `differences`. Each difference
identifies its `field`, `expected`, `observed` and `reason`; malformed or oversized
values are bounded. The result does not return an arbitrary raw ERP document or
upstream exception body. It preserves the mock/live distinction with `simulated`.

Status interpretation:

- `verified`: the returned draft matched the persisted contract for this read.
  It is not an approval, a reconciliation decision or a permission to replay.
- `missing`: no matching record was returned. ERP may still have committed.
- `mismatch`: returned values or the document contract did not match.
- `blocked`: the persisted target/snapshot was invalid or the local ledger changed
  during the read; the observation cannot establish the held operation's result.
- `unavailable`: the read failed or the response could not be used. Retry only the
  read after reviewing the cause; never substitute an external create.

The service rechecks reader authority and local ledger consistency after ERP I/O.
All returned observations keep `external_write_attempted=false` and
`replay_permitted=false`. A successful read does not turn either flag into a grant.

## Native UI

The recovery view lists held operations, shows their local evidence and offers an
explicit read-only ERP comparison. Local diagnostics remain distinct from an ERP
observation. The UI must not infer a successful comparison from an old response,
reassign it to another selected operation, or retain protected results after a
session/scope change. Loading, empty, error and unavailable states remain visible.
There is no resume, release hold, acknowledge reconciliation or replay button.

## Exact-version backup compatibility

Adding `recovery.py` changes the executable-source fingerprint used by the
version-1 backup format. A `.pfb` produced by baseline `a10f62b` must still be
restored with matching trusted baseline code and the required schema. This
increment does not make baseline backups directly restorable by the new code,
provide a backward-compatible backup migration, or relax exact fingerprint
checks. A successful new-code backup/restore drill proves only its own
version-matched path. Plan any subsequent code upgrade as a separately reviewed
procedure; do not disable the check or edit/re-hash backup metadata to force an
old archive through it.

## Evidence and verification status

The accepted baseline archive is
`ProcureFlow-a10f62b-01-源码与验收摘要.zip`. Its
`ci-final/workflows-and-jobs.json` records six successful runs and fourteen
successful jobs, including repeated push/PR gates. The compact
[baseline evidence index](evidence/a10f62b-baseline.json) preserves exact source
member hashes and workflow links. The source ZIP's 322 tracked files were compared
byte-for-byte with the baseline Git tree during this documentation update.
Part 01 contains the source and summaries; the full raw CI files are distributed
across the other parts of the accepted package. Reading these summaries is not a
new CI execution or a new independent security review.

The root `MANIFEST.json` is now a provenance pointer, not a checksum inventory of
this evolving tree. Its baseline fields do not claim to name the current commit.
The old a2 source-package inventory is preserved byte-for-byte under
[docs/history](history/MANIFEST-0.1.0a2.json); its hashes apply only to that
historical package. It is separate from the accepted multi-part evidence
archive's own `MANIFEST.json`.

### Local results recorded on 2026-10-08 (UTC)

The machine-readable [local verification summary](evidence/stage16-local-verification.json)
lists the raw-record filenames and SHA-256 values from the accompanying delivery.
These are local results on Python 3.12.14 and Node 24.19.0, not fresh CI results:

| Check | Result | Record |
| --- | --- | --- |
| Full non-PostgreSQL/non-browser Python regression | 1,782 passed; 15 deselected; 0 failed/errors/skipped; one AnyIO deprecation warning | `final-core.log`, `final-core.xml` |
| Focused recovery diagnostics | 47 passed, included in the 1,782 above | `recovery-diagnostics.log`, `recovery-diagnostics.xml` |
| Frontend units | 56 passed, including 13 new recovery cases | `frontend/unit-final.log` |
| Type check and production build | Passed; type check also passed after restoring `next-env.d.ts` | `frontend/typecheck-final.log`, `frontend/typecheck-restored-env.log`, `frontend/build-final-telemetry-disabled.log` |
| API contract drift | Passed | `contracts.log` |
| HTTP/worker smoke | Passed; 16 named steps, mock ERP | `smoke.log` |
| Real-HTTP frontend transport | 5 passed | `web-http.log` |
| Offline maintenance CLI | Scoped `inspect` succeeded on a fresh migrated, paused synthetic DB; missing tenant rejected; DB unchanged and 0 ERP writes | `cli-acceptance.log` |
| Synthetic SQLite paired recovery | Passed; 2 source documents verified, 0 external writes, no automatic replay | `recovery-acceptance.json`, `recovery-acceptance.log` |
| Native browser gate | Blocked before UI execution on both attempts | `frontend/native-gate/`, `frontend/native-gate-escalated/` |
| Current-increment live PostgreSQL, containers, live ERPNext and remote CI | Not run | No current pass claimed |

The recovery drill additionally verified that diagnostic GETs leave the database
unchanged, offline inspection works while recovery is paused, restored old
credentials cannot read diagnostics, and reviewed resume keeps the old operation
held. This uses synthetic SQLite and MockERP; it does not establish a live ERP
recovery result or PostgreSQL parity for this increment.

The strict native gate collected 25 cases (21 existing workbench, one retention,
and three recovery cases), but Chromium launch failed with
`CHROMIUM_UNIX_SOCKET_DENIED` / `socket() Operation not permitted`. Both recorded
attempts stopped before UI assertions, producing setup errors rather than browser
passes. No screenshots were created. The successful build, Node units and HTTP
checks do not establish native UI acceptance; the browser gate remains blocked.
No further browser bypass attempt is part of this verification.

Test sets overlap and contain parameterized cases. In particular, the focused 47
must not be added to 1,782, and none of these counts is a number of independent
procurement tasks or a real-model quality score. The final delivery's source
commit/tree must be recorded externally to this document; a later code change
requires new verification.

No production recovery, encrypted or signed backups, OCR, multi-item procurement,
real-model quality/cost result or independent security certification is claimed.
