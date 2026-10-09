# Stage 17 publication and CI follow-up

The user authorized publication to the existing PR #1 and complete CI, including
fixes and reruns of failures introduced by this increment. The branch was updated
without force, using the expected head `fc9750eeb5e0bf7417f16ba981a8c3bee891ebdf`.
No merge, deployment, production data, paid model call or security-review retry
is included.

## Exact first publication

- Published commit: `48af996efbd91072ef59ed4a5bd55843388a64b4`
- Tree: `672fc3bd140b64eef9d455b01d708b7736523f3a`
- Original local commit: `c55fdeddb66653c1f0f87295774047d7dcb01dc8`
- The published tree is byte-identical to the approved local source. The original
  local bundle and raw local evidence remain immutable.
- [PR #1](https://github.com/volcano-code/ProcureFlow/pull/1) stays open.

## First-run failure and narrow correction

The native production-build browser run executed all 32 cases: 31 passed and
one new test failed. `test_native_multi_item_request_line_edit_stales_approval_and_preserves_snapshot`
incorrectly expected a retained-proposal banner after a request edit. The existing
service intentionally clears the active proposal and retains immutable approval
history. The corrected test requires a null active proposal, no approval/reject/
execute controls, updated request quantity and an unchanged historical snapshot.
No application authorization, approval binding or readback check was relaxed.

The real-ERP workflow path filter now also includes `apps/web/**`, covered by a
selection regression. A native-only correction therefore triggers both real-ERP
database matrices on the new head instead of relying on evidence from an older
commit. Its runner and evidence checker both explicitly require the ninth,
two-SKU scenario.

## Evidence and claims

Historical first-head run records:

- [Core, native UI, PostgreSQL and Compose (push)](https://github.com/volcano-code/ProcureFlow/actions/runs/37883623592)
- [Core, native UI, PostgreSQL and Compose (PR)](https://github.com/volcano-code/ProcureFlow/actions/runs/37883627626)
- [Native container (push)](https://github.com/volcano-code/ProcureFlow/actions/runs/37883623489)
- [Native container (PR)](https://github.com/volcano-code/ProcureFlow/actions/runs/37883627638)
- [Nine-scenario isolated real ERP, both databases](https://github.com/volcano-code/ProcureFlow/actions/runs/37883623479)
- [Native pilot real ERP, both databases](https://github.com/volcano-code/ProcureFlow/actions/runs/37883623528)

These first-head runs are not final-head acceptance. Check the current commit's
complete Actions runs and accompanying delivery manifest for exact SHA/tree,
original artifact IDs and SHA-256 digests, JUnit counts, per-SKU evidence checks,
failures and reruns. A checked-in manifest cannot identify its own commit hash;
the final identity is recorded externally when packaging the verified source.

Real-ERP acceptance uses fresh disposable ERPNext/MariaDB and synthetic records.
The expanded HTTP scenario proves a two-line CNY/EA draft with zero tax and
discount, freight once, lost-response recovery and independent readback. Native
multi-item browser coverage uses mock ERP. The separate native pilot real-ERP
gate retains its existing single-item scenarios; do not label it a native
multi-item real-ERP acceptance or infer a general FX/UOM/tax integration.

Original local counts and blocked local browser attempts remain in
`docs/evidence/stage17-local-verification.json`. Its hashes and earlier evidence
files are preserved. The original local root manifest is archived byte-for-byte
in `docs/history/MANIFEST-c55fded.json`.
