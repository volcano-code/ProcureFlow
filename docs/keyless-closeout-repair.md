# Keyless closeout repair: source-clean packaging and strict evidence

Continues remote `2cd03da88c3e4936f334f090a70c3b0b6e27b3eb`. No production
API, authority, extraction behavior, migration or model credential is changed.

## Actual defects reproduced

The old readiness guard accepted 34 unrelated or duplicate test names and even a
JUnit summary declaring a failure when no child failure element was present.
The operation record checker also accepted empty/non-string IDs on the denied
operation. Eleven adversarial tests failed against the previous implementation,
then passed after the repair. These are validation defects, not evidence that
production procurement was compromised.

The workflow also downloaded receipts into the checkout immediately before
invoking a packager that correctly requires a clean checkout. Receipts and final
outputs now live under RUNNER_TEMP. The clean-source guard is unchanged.

## Implemented checks

* `scripts/joint_tests.json` fixes the 34 exact joint checker test names and the
  SHA256 of their source file. `joint_evidence.check_junit` requires all names,
  correct module, matching summary counts, no duplicates/failures/errors/skips,
  bounded XML and no DTD/entity declaration. Updating those tests requires an
  explicit manifest update. This checks evidence consistency, not authenticity.
* Each replica records source commit/tree, repository, Actions run/attempt,
  replica and its exact disposable project name. Only these allowlisted values
  are recorded, never the environment. Readiness requires replicas 1/2/3 from
  the same run/attempt and rejects reused operation or candidate IDs.
* The independent PostgreSQL audit binds each imported quotation and every
  field's evidence to its own candidate/document/hash; empty provenance, links
  to another/cancelled task, changed values and missing confirmation fail.
* A branch-scoped push trigger supplements the existing PR/manual triggers.
  Replicas run sequentially with fail-fast disabled and retain individual
  outcomes. Permissions stay contents:read; no secret or paid job is introduced.

Adding a trigger does not prove the event was delivered. The initial new commit
`63ab8c9` was written and read back, but the Actions query still returned zero
records at the time it was checked. The cause of the earlier missing events and
stale PR metadata is not established. No protection/approval gate is disabled.

## Reproduce without a model key

```sh
python -m pytest -q tests/test_candidate_procurement.py tests/test_closeout.py \
  tests/test_closeout_regressions.py tests/test_joint_evidence.py tests/test_grouped_candidates.py
python scripts/closeout.py data-audit --output /path/to/NEW-grouped-report.json
```

The complete workflow additionally runs the two actual candidate runtime tests;
those require the pinned application dependencies. A local dependency failure is
not converted to a pass. Real joint browser/PostgreSQL/ERP execution still needs
the disposable runtime environment, not a fixture-produced success receipt.

For locally collected genuine pytest records, run the exact test file (without
-k selection) to produce JUnit before checking it. Execution.json is created only
inside Actions by `joint_evidence.py`; do not fabricate it to certify local runs.
Old receipts without this metadata intentionally cannot close the new gate.

## Completion boundaries

This change repairs keyless code and evidence collection. Real B/C quality,
actual human semantic review, external users and reviewed product release remain
separate. No participant/annotation is invented; scripted-provider results are
never live-model quality or an unseen Holdout. A successful keyless gate cannot
set product_release_approved to true.
