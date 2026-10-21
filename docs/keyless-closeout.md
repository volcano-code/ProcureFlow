# No-key closeout: implementation and acceptance boundaries

This increment continues cb65043; it does not replace earlier source or silently
complete R1/R2. The operator's real model key is neither read nor needed by the
new joint gate. User observation and independent semantic review still require
real people; they cannot be supplied by a test fixture.

## Joint business gate

`verify_candidate_procurement.py --ephemeral-test` is destructive **only to its
new marked synthetic lab**. Never point it at shared/pilot/company data. It
requires the existing disposable ERP nonce/credentials and an explicitly
allowed PostgreSQL test database. A new random business schema is allocated and
removed by the existing test database context. It builds and runs native Next,
API and two independent Python Workers. The model endpoint is a loopback scripted
protocol fixture with no upstream; ERPNext/MariaDB and PostgreSQL are real.

The browser does not intercept business HTTP responses. Independent pilot login
contexts act as buyer and approver; these are program-driven synthetic human
roles, not user research. Normal and receipt-loss cases import a candidate as
UNCONFIRMED, explicitly confirm fields, calculate with the deterministic engine,
approve as another user, execute and read back real ERP Supplier Quotation drafts.
Unconfirmed analysis, unapproved execution and buyer self-approval are rejected.
A revoked approver prevents dispatch; queued candidate cancellation prevents a
provider request. API restarts retain queue and operation identities. Actual
queries of the PG ledger and the ERP database cross-check outcomes and evidence.

Expected per fresh run: four candidate tasks, three manually imported/confirmed
quotes, three approvals/operations, two completed draft operations and one denied
operation. Two ERP POSTs create exactly two draft quotations, no submitted
quotations or Purchase Orders. Six scripted provider requests, zero real-model
requests. Three separate CI replicas are independent disposable environments;
they are not three different task sets, a reliability percentage, or a natural
concurrency/lease test. Results must be observed on the exact final tree.

No API/frontend model key; candidate Worker has no ERP key; procurement Worker
has no model key. Only fixed counts, IDs and sanitized phase/frame diagnostics are
exported. No session tokens, raw private HTML, provider response bodies or live
credentials enter artifacts. Existing safety/nonce/role checks remain in place.

## Grouped data and actual quality

`evals/candidate60` contains public grouped extraction inputs, not a private or
unseen benchmark. The original20 are unchanged;40 new inputs cover different
source layouts, missing/conditional terms and contradictions. Input validation,
reference-answer structure, baseline performance and live model performance are
separate facts. Default liveB/C runs stay BLOCKED before reading credentials.
No gain, currency cost or semantic correctness is fabricated for blocked runs.

## Reproduce selected no-key engineering checks

The `.github/workflows/keyless-closeout.yml` workflow is the exact isolated runtime
recipe. It uses the existing locks and does not publish ports beyond the runner's
local ephemeral services. Full joint testing needs Node/Docker/PostgreSQL and the
real ERP image; unit tests alone cannot substitute for that execution.

```bash
python -m pip install -r services/api/requirements-dev.txt
python -m pytest -q tests/test_candidate_procurement.py tests/test_grouped_candidates.py tests/test_closeout.py
python scripts/closeout.py data-audit --output evals/reports/local/NEW/grouped.json
```

After downloading the three **same-tree** original CI records to separate dirs:

```bash
python scripts/closeout.py readiness \
  --joint /path/to/replica1 --joint /path/to/replica2 --joint /path/to/replica3 \
  --output evals/reports/local/NEW/readiness.json
```

Missing, stale, repeated-operation or skipped-test records cannot pass. This is
consistency checking of existing evidence, not a second runtime execution or
cryptographic/independent security attestation. Even success sets
`product_release_approved=false` and keeps explicit external requirements.

## Packaging and reviewed release

From a clean checkout, prepare a new directory outside the repository:

```bash
python scripts/closeout.py package --output ../NEW-procureflow-release
```

Only Git-tracked regular files are archived; symlinks, tracked databases, private
environment files and runtime directories are rejected. The manifest binds exact
commit/tree, archive SHA256 and individual files. Ignored local `.data` is never
included. This is not a comprehensive secret scanner or automatic production
release; no tag/merge/deployment happens. Existing a2 compatibility version strings
and historical reports are not rewritten into fictitious newer achievements.

Review stacked branches in dependency order: existing R1 baseline, candidate
Worker, extraction quality, method comparison, then this increment. Re-run final
HEAD checks after any merge/rebase and inspect migration/rollback instructions.
Never force-push over user work or mark failed checks optional to publish.

## Remaining acceptance outside this keyless implementation

1. Real provider B/C trials with agreed budgets; actual failure fixes and six
   representative live-model procurement scenarios. A key is not a quality pass.
2. Actual independent semantic annotation and calibration. The prior private
   review tooling initializes labels blank; never prefill them from gold.
3. An external, genuinely unseen corpus if generalization is claimed. The public
   Evaluation partition here cannot serve as an independently unseen Holdout.
4. External users and maintainer review; no fabricated user feedback or review.

For a small supervised user trial, recruit3-5 consenting participants and give
each2-3 synthetic tasks: missing freight, changed quote requiring re-approval,
and receipt-loss readback. Record pseudonymous participant ID, task/version,
start/end, completion or block, human edits and an optional comment. Do not ask
for real procurement documents, personal secrets or model keys. Keep session
recordings private and optional. Report all failures and the small sample size;
these tasks do not constitute production deployment or representative research.

A demonstration should show one successful path and one missing/uncertain case,
state which model is scripted/live, and identify the delivered commit and actual
score evidence. No claim of being perfect or having only a key left is implied.
