# Candidate comparison and human review — completion batch

Base: `c024f7549621e2c19cbf2fba9fab66d9771e968e`. This batch does not
merge PRs, perform real-model calls, or certify R1/R2. Historical success counts
belong to their exact tested commit. Existing business authorization, migrations,
UI, ERP and model candidate contracts are retained.

## Implemented

B is fixed retrieval + one model request. It reads the same scoped request and
all bound source fragments through CandidateService, then uses the SAME
CandidateProposal validator and post-processing. No callable tools are sent to
the provider, no repair/retry is performed. Missing evidence, tool calls,
truncation, malformed JSON and missing usage fail closed. C retains the current
bounded adaptive agent. Neither B nor C imports/confirms/approves quotes or calls
ERP in the evaluation runner. Scripted fixtures measure protocol only.

`candidate20` extends, not edits, the existing 12 tasks. It verifies the base
input/gold fingerprints, then appends eight developer-authored tasks: bilingual
brief, withdrawn price, conditional freight, delivery conflict, Unicode IDs,
untrusted appendix, missing currency and unspecified discount. All are public
synthetic **Dev**, single-source text. This is not 20 unseen families, not a
Holdout, not PDF/XLSX coverage and not the separate procurement Dev12. Reference
structure tests do not constitute independent review of answer semantics.

A/B/C comparison rejects mixed source, code, model, endpoint, thinking mode,
contract/context protocol or time/token ceilings. B has exactly ONE attempted
request per case, C up to FOUR. Both have the same 12-tool/45-second/16,000
reported-token/1,600-output-token ceilings; source retrieval is fixed vs dynamic
and routing prompts differ. This is a method comparison, not a pure prompt
ablation or equal-request-count claim. Latency includes local source preparation.
Blocked/partial runs cannot produce a net-improvement number. Usage is provider
reported, never a hard currency cap. Single-source extraction is not end-to-end
procurement success.

## Offline commands

```bash
python -m pip install -r services/api/requirements-dev.txt
python -m pytest -q tests/test_candidate_quality.py tests/test_candidate_methods.py
RUN="evals/reports/local/methods-$(date +%s)"
python scripts/compare_candidate_methods.py validate-gold --output "$RUN/gold.json"
python scripts/evaluate_candidates.py run --suite evals/candidate20 --mode baseline --output "$RUN/A.json"
python scripts/evaluate_candidates.py grade --suite evals/candidate20 --observations "$RUN/A.json" --output "$RUN/A-scores.json"
```

Default B/C runs without `--allow-network` return exit 2 with zero network
attempts BEFORE reading credentials. The new CI runs only offline tests and
scripted protocols; there is no automatic paid job in it.

## Explicit, private live execution

Configure `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`,
`PF_EVAL_APPROVED_HOST`, optional `LLM_THINKING_MODE` in a trusted environment.
Keep all live runs on the same exact code/model/configuration. No credentials in
Git/chat/command arguments. No fallback provider or automatic retry.

```bash
# After completing A above. Create only the private parent, not the child dirs.
umask 077
PRIVATE_PARENT="$HOME/procureflow-private-review"
mkdir -p "$PRIVATE_PARENT"
python scripts/evaluate_candidates.py run --suite evals/candidate20 --mode live \
  --strategy single_shot --allow-network --max-requests 20 \
  --review-dir "$PRIVATE_PARENT/B-$(date +%s)" --output "$RUN/B.json"
python scripts/evaluate_candidates.py run --suite evals/candidate20 --mode live \
  --strategy agent --allow-network --max-requests 80 \
  --review-dir "$PRIVATE_PARENT/C-$(date +%s)" --output "$RUN/C.json"
python scripts/compare_candidate_methods.py compare --baseline "$RUN/A.json" \
  --single-shot "$RUN/B.json" --agent "$RUN/C.json" --output "$RUN/paired.json"
```

These are two separate explicit authorizations: at most 20+80 provider attempts,
not one global concurrent/currency budget. If a run blocks/fails, inspect its
fixed status before deliberately authorizing another; do not retry until
uncertain provider outcomes have been accounted for. The provider can still
charge for interrupted requests. Live method scores remain UNKNOWN until run.

## Local semantic review

`--review-dir` must be a NEW directory OUTSIDE the repository. It is reserved
before networking, mode 0700. The packet and blank annotations are exclusively
created as 0600 files. No packet is exported in CI artifacts. OS permissions are
not encryption. Public observations retain fingerprints, not model prose.

The packet records the actual proposed values, bound synthetic source, cited
lines and clarification question; no gold answers, hidden reasoning or provider
credentials. It is DATA, never executable instructions. For every row, a human
fills verdict and a short justification, reviewer_id, reviewer_role (developer
or external_reviewer), and attest_manual_review=true. Known-field verdicts:
supported/unsupported/unclear; unknowns: appropriate_unknown/inappropriate_unknown/
unclear. All default to null. Do not auto-fill labels from gold or model scores.

```bash
python scripts/compare_candidate_methods.py review --observations "$RUN/C.json" \
  --packet "$PRIVATE_C/packet.json" --annotations "$PRIVATE_C/annotations.json" \
  --output "$RUN/C-human-review-summary.json"
```

Set PRIVATE_C to the actual newly created C directory. Packet/value/source and
observation fingerprints must match. Missing ratings, altered values or wrong
source fail; incomplete review has null semantic_support_rate. A reviewer name
and checkbox are self-attestation, not authenticated identity, expert consensus
or external certification. Review coverage and abstentions remain visible.

## Remaining acceptance, not a moving feature wishlist

- Execute authorized B/C live tasks, inspect failures and calibrated semantic
  review; no claim that credentials alone make quality pass.
- Separate grouped 60-task evaluation and genuinely unseen Holdout remain open.
- Native UI + PostgreSQL + live model + independent approval + real ERP joint
  acceptance and external-user trials remain open; do not splice separate gates.
- Inspect final exact-head CI, review dependency PRs, perform upgrade/rollback
  checks and release documentation before merging/tagging. No release here.

Protocol reference checked for this batch: DeepSeek JSON Output guide
https://api-docs.deepseek.com/guides/json_mode/ . JSON mode does not replace schema,
evidence or semantic validation. Models remain operator-configured, not a
hard-coded claimed latest model.
