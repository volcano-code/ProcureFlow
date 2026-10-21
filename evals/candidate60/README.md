# Grouped candidate extraction corpus v1

60 **public, developer-authored synthetic single-text extraction tasks**. This is
not 60 full procurement scenarios, independently reviewed gold, or an unseen
Holdout. The reserved partition is named **Evaluation**, not Holdout.

The original 20 task/source/gold records are referenced unchanged. Forty new
records are stored in separate input/gold additions. A fingerprinted manifest
assigns 34 declared source families to Dev30/Validation10/Evaluation20. Supplier
identities, exact source duplicates and number-normalized cross-partition text
are checked. These mechanical checks do not establish that semantic families
are perfectly independent. Family labels and reference answers still require
external review before generalization claims.

`inputs.json`/`gold.json` under each partition are descriptors. Load them through
`evals.candidate_quality.load_inputs/load_gold` or the existing CLI, not by
assuming that the descriptor contains inline cases. Input loading never reads
gold. Gold is used only by validation/grading, never by prompts or model tools.
The module returns explicit public/unseen=false labels with the partition.

```bash
python scripts/closeout.py data-audit --output evals/reports/local/grouped-audit.json
python scripts/evaluate_candidates.py run --suite evals/candidate60/dev \
  --mode baseline --output evals/reports/local/dev-A.json
python scripts/evaluate_candidates.py grade --suite evals/candidate60/dev \
  --observations evals/reports/local/dev-A.json --output evals/reports/local/dev-A-score.json
```

All output paths must be new. Use new run directories rather than replacing old
attempts. Scores from Dev, Validation and Evaluation are reported separately;
re-running the same tasks is not new independent data. Do not pool this corpus
with the previous procurement Dev12, extraction Dev12/20 or runtime fixtures.

B/C live execution remains explicit opt-in with the existing HTTPS host and
server-side provider configuration checks. Default 30-task C budget remains80
requests (not silently increased to120); a larger budget up to120 needs explicit
`--max-requests`. Budget exhaustion is recorded, not removed from the denominator.
The same selected budget must be declared in comparable runs. Actual currency
cost and semantic review cannot be inferred from request counts or reference IDs.
