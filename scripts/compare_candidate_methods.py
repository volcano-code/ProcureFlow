"""Offline A/B/C comparison and local human-review summaries; NEVER calls a model."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.candidate_quality import load_inputs, load_json, write_json, require
from evals.candidate_methods import compare_methods, validate_gold
from evals.candidate_review import summarize_review


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('validate-gold', 'compare', 'review'))
    p.add_argument('--suite', type=Path, default=ROOT/'evals/candidate20')
    p.add_argument('--baseline', type=Path)
    p.add_argument('--single-shot', type=Path)
    p.add_argument('--agent', type=Path)
    p.add_argument('--observations', type=Path)
    p.add_argument('--packet', type=Path)
    p.add_argument('--annotations', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args(argv)
    try:
        if args.command == 'validate-gold':
            result = validate_gold(args.suite); code = 0
        elif args.command == 'compare':
            require(all((args.baseline, args.single_shot, args.agent)), 'REPORTS_REQUIRED')
            result = compare_methods(args.suite, *(load_json(f) for f in (args.baseline, args.single_shot, args.agent)))
            code = 0 if result['real_model_comparison_complete'] else 2
        else:
            require(all((args.observations, args.packet, args.annotations)), 'REVIEW_FILES_REQUIRED')
            result = summarize_review(load_inputs(args.suite), load_json(args.observations),
                                      load_json(args.packet), load_json(args.annotations))
            code = 0 if result['status'] == 'review_record_complete' else 2
        write_json(args.output, result)
        print(json.dumps({'status': result['status'], 'exit_code': code}))
        return code
    except (ValueError, TypeError, KeyError, OSError):
        print('{"status":"failed","reason":"INVALID_COMPARISON_OR_REVIEW"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
