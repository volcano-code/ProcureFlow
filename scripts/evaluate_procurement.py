"""Run the ten frozen synthetic development cases, offline only.

This command has no live mode and never reads model credentials. Hand-authored
HTTP replays exercise the real service and LangGraph; fixture rates are not model
quality. Use --review-packet for an independently rated explanation rubric.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', action='store_true', help='Explicitly run offline scripted development fixtures')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--review-packet', type=Path, help='Export synthetic candidates, without oracle or automated scores')
    parser.add_argument('--reviews', type=Path, help='Independent ratings bound to the frozen suite and candidate hashes')
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        # No app import, DB setup, or dependency initialization under ambient settings.
        os.environ.clear()
        from procureflow.evaluation import run_suite
        config = json.loads(sys.stdin.read(200_001))
        result, packet = run_suite(ROOT / 'evals/procurement', config.get('reviews'))
        print(json.dumps({'report': result, 'packet': packet}, ensure_ascii=False))
        return 0 if result['status'] == 'passed' else 1
    if not args.fixture:
        print(json.dumps({'status': 'blocked', 'reason': 'EXPLICIT_FIXTURE_OPT_IN_REQUIRED', 'network_attempted': False}))
        return 2
    try:
        reviews = None
        if args.reviews is not None:
            data = args.reviews.read_bytes()
            if len(data) > 180_000:
                raise ValueError('Review bundle too large')
            reviews = json.loads(data)
        with tempfile.TemporaryDirectory(prefix='pf-evaluation-worker-') as directory:
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--_worker'],
                input=json.dumps({'reviews': reviews}), text=True, encoding='utf-8', capture_output=True,
                cwd=directory, env={'PYTHONPATH': str(ROOT / 'services/api'), 'PYTHONIOENCODING': 'utf-8'},
                timeout=240, check=False)
        if completed.returncode not in (0, 1) or len(completed.stdout) > 200_000:
            raise ValueError('Invalid worker result')
        bundle = json.loads(completed.stdout)
        result, packet = bundle['report'], bundle['packet']
        if result['status'] not in {'passed', 'failed'} or (completed.returncode == 0) != (result['status'] == 'passed'):
            raise ValueError('Inconsistent worker result')
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print(json.dumps({'status': 'failed', 'reason': 'EVALUATION_OR_REVIEW_INVALID', 'network_attempted': False}))
        return 1
    for path, value in ((args.output, result), (args.review_packet, packet)):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(main())
