# Stage 18 publication and exact-head acceptance

The approved local source `f70388ffa16345e1f439cdbdfa111462619ae8b6`
(tree `f8215641fc6894dfaacb395716d01d7233a2e902`) was published unchanged as
[`ea09ffaa2601e000a86c2427747e9ac4be66310e`](https://github.com/volcano-code/ProcureFlow/commit/ea09ffaa2601e000a86c2427747e9ac4be66310e).
The connector-created commit has different author/time metadata; its complete
Git tree is identical to the reviewed local artifact. Publication used a normal
fast-forward with expected old head `a35f78713777016e8d76338391b18e0d558aa850`.

[PR #1](https://github.com/volcano-code/ProcureFlow/pull/1) remains open. Main stays
at `780945123bfa907475b164640096c85a63db60b2`. There is no merge, deployment,
production backup/restore, real backup-key provisioning or independent security
review in this work.

## CI selection follow-up

The first public tree retains the exact originally reviewed selection. Its core
suite includes all 278 new protection tests, but the standalone recovery drill
runs only `plain` and `signed-encrypted`, and the explicitly selected PostgreSQL
suite does not yet exercise protected roundtrips. A follow-up adds:

- Separate standalone synthetic recovery receipts for all four protection modes
- Four explicitly selected PostgreSQL-backed backup/restore cases, one per mode,
  using only a disposable fixture database and new random restore schemas
- Checks of evidence equality, target/source isolation, RECOVERY pause, revoked
  authority, provenance receipts and unchanged held uncertain-operation evidence
  after reviewed resume

Existing browser, dependency/install, native/container and isolated real-ERP
regressions remain required. PostgreSQL evidence must come from actual
PostgreSQL-backed cases; extra SQLite cases do not establish PostgreSQL parity.
The existing live ERP workflows use synthetic disposable lab accounts and data;
they do not provision a real user's backup keys or operate on production data.

## Evidence rule

The original [local evidence index](evidence/stage18-local-verification.json)
remains a historical record, including its 15 local exclusions. It is not
rewritten as remote proof. Source fingerprints still bind executable package and
migration files, and V1 backup source/schema compatibility remains exact.

A workflow definition, queued/in-progress result or earlier-head success is not
a final pass. Final delivery must identify its exact public commit/tree, each
workflow event and job result, and the downloaded original artifact hashes.
Counts from push/PR duplicates and overlapping test collections must not be
summed as unique procurement tasks. CI logs and artifacts may include technical
paths; no key material or production records are intentionally exported.

No final-current-head CI conclusion is asserted by this page itself. Consult the
source-bound final delivery record and linked Actions runs.
