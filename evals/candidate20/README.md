# candidate-extraction-dev20-v1

Frozen extension of candidate-extraction-dev12-v1: first 12 cases/answers are
loaded only after verifying their fingerprints; eight additional sources and
reference answers are stored separately. Every task is synthetic and public
Dev, with developer-authored, not externally reviewed gold. No Holdout claims.

Use `scripts/evaluate_candidates.py --suite evals/candidate20` for execution and
`scripts/compare_candidate_methods.py validate-gold` for reference structure.
This suite does not assess policy selection, multi-source supersession, real
PDF/XLSX parsing or ERP execution. Do not combine its denominator with the
separate procurement Dev12 or count repeated runs as new tasks.
