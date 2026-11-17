# Candidate extraction quality — isolated real-provider entry point

This continuation starts from `af3e54d`. It adds an extraction-only Dev experiment,
not a production workflow rewrite, a new ERP gate, or a live-model success claim.
The existing 12-task procurement suite remains unchanged. Its denominator and
results must not be combined with this new `candidate-extraction-dev12-v1` suite.

## What is actually measured

Twelve synthetic TXT sources: structured EN/CN; nonstandard EN/CN quotations;
missing freight/tax; conflicting price/ambiguous quantity; instructions embedded
in source documents. Gold is developer-authored, public, not independently
reviewed and not a Holdout. Each input carries a SHA-256 and family label.

A: actual current conservative parser on exactly the same source bytes.
C: actual CandidateService reserve/enqueue -> candidate_worker.drain_once ->
CandidateAgent, in a fresh isolated process/SQLite database per task. No gold is
passed to the worker, prompt, tools or provider. The task does not import a quote,
confirm it, approve it or create an ERP operation. Actual DB counts are checked.
This is not an HTTP/browser/PostgreSQL test; prior lifecycle gates cover those.
A single-shot model B and wider procurement task comparison remain future work.

The grader uses independent frozen values, required clarification fields/reasons
and curated supporting source lines. Decimal equality is exact after normalized
string conversion; opaque supplier/SKU identity remains case-sensitive. Public
records contain value hashes and line hashes, not model prose, raw provider
errors, keys, or reasoning_content. Gold-shaped grader tests are not model scores.
Location validity, gold-span agreement and semantic support are separate: general
semantic_support_rate remains null pending independent review.

Every task stays in the denominator. Failed/unsupported/blocked cases are not
removed; no repeated request is used to select a best answer. Partial/live-blocked
comparisons have no net-improvement number. Even a green offline CI means only
engineering/protocol/grader acceptance, NOT that C outperforms A.

## Commands (new output path each time)

```bash
python -m pip install -r services/api/requirements-dev.txt
RUN="evals/reports/local/candidate-quality-$(date +%s)"
python scripts/evaluate_candidates.py validate --output "$RUN/inputs.json"
python scripts/evaluate_candidates.py run --mode baseline --output "$RUN/baseline.json"
python scripts/evaluate_candidates.py grade --observations "$RUN/baseline.json" --output "$RUN/baseline-scores.json"
```

Default `run --mode live` blocks before reading credentials/constructing clients.
Only after explicitly authorizing provider charges, securely configure
`LLM_BASE_URL` (HTTPS), `LLM_MODEL`, `LLM_API_KEY`, and `PF_EVAL_APPROVED_HOST` (exact
hostname). Use current provider model IDs; none are hardcoded. Optional
`LLM_THINKING_MODE` is default/enabled/disabled. Keys must not be pasted into chat,
source, shell command history, screenshots or output files. Use local secret
injection or the controlled CI environment, not command-line credential arguments.

```bash
python scripts/evaluate_candidates.py run --mode live --allow-network --max-requests 48 --output "$RUN/live.json"
# Run grading separately even if live exits 1 (partial/failure) or 2 (blocked).
python scripts/evaluate_candidates.py grade --observations "$RUN/live.json" --output "$RUN/live-scores.json"
python scripts/evaluate_candidates.py compare --baseline-observations "$RUN/baseline.json" --observations "$RUN/live.json" --output "$RUN/paired.json"
```

Use `--require-all-success` on grade only for an explicit all-tasks quality gate.
A baseline run exiting 0 means measurement completed, not that all tasks passed.
Provider/schema errors remain failed observations. No key or invalid configuration
produces 12 blocked cases and zero network attempts, never a fixture fallback.

## Budgets and safety

At most 4 provider attempts, 12 tool calls, 1600 requested output tokens per call,
16000 reported tokens and 45 model seconds per case; the parent kills a case after
65 wall seconds including startup. At most 48 provider attempts in one full run.
Remaining capacity is reserved conservatively before starting a case. No HTTP
retry, fallback provider or task replay. An ambiguous outcome or missing usage
stops subsequent live tasks. Cancellation/termination does not guarantee a
provider has not processed or billed a request. These are NOT hard currency caps;
usage is provider-reported and cost stays null. Separate executions do not share
a global billing ledger. `--limit` may reduce tasks but unrun cases remain blocked.

The worker receives explicit config over stdin, not argv/environment. It clears
ambient app/auth/ERP/proxy/tracing settings and always creates a new directory.
Only a trusted, explicitly approved HTTPS provider hostname is accepted in live
mode; loopback fixtures cannot become live scores. This is an application scope
boundary, not an OS/network sandbox or protection from operator DNS misconfiguration.

## GitHub Actions

`candidate-quality.yml` runs offline on the feature branch/PR. The paid job is
manual only (`workflow_dispatch`, `allow_live=true`), needs the offline job, and
references Environment `procureflow-model-eval`. Configure required reviewers,
branch restrictions and environment credentials in GitHub before using it. Merely
referencing that name does not automatically create protection rules. The manual
workflow may need to be reviewed/registered on the default branch before the Run
workflow UI is available; do not merge an unreviewed change just to bypass this.

Only `LLM_API_KEY` is an Environment secret; the URL/model/approved host and
optional thinking mode are Environment variables. No secret is provided to PR
jobs. This change does not read repository secret values, configure reviewers,
trigger a paid run, merge a PR or deploy publicly.

Provider/reference notes (checked 2026-10-21): DeepSeek JSON Output requires an
explicit JSON instruction and reasonable output cap; malformed/empty/truncated
output remains a failure. GitHub environment secrets are available only after
configured protection rules pass. References:
- https://api-docs.deepseek.com/guides/json_mode/
- https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
