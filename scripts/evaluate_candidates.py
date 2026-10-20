"""Synthetic extraction evaluation. No default network, no ERP, no automatic retry.

The live runner reuses CandidateService enqueue + candidate_worker.drain_once in
an isolated per-task process/SQLite database. This is not a new browser/PG gate.
The parent reads inputs only; grade is a separate command that reads gold.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'services/api'))
from evals.candidate_quality import load_inputs, load_json, fingerprint, write_json, grade, require, validate_record, compare
from evals.candidate_trial import empty_record

DEFAULT_SUITE = ROOT / 'evals/candidate12'


def implementation_hash():
    paths = sorted((ROOT / 'services/api/procureflow').glob('*.py'))
    paths += [ROOT / x for x in ('evals/candidate_quality.py', 'evals/candidate_trial.py', 'scripts/evaluate_candidates.py')]
    return fingerprint({p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def live_config(allow_network, env=None):
    if not allow_network:
        return None, 'EXPLICIT_NETWORK_OPT_IN_REQUIRED'
    env = os.environ if env is None else env
    names = ('LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'PF_EVAL_APPROVED_HOST')
    if not all(env.get(k) for k in names):
        return None, 'MODEL_CONFIGURATION_REQUIRED'
    url, key, model, host = (env[k] for k in names)
    thinking = env.get('LLM_THINKING_MODE', 'default')
    try:
        parsed = urlsplit(url)
        require(parsed.scheme == 'https' and parsed.hostname == host and parsed.port in {None, 443}
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                'INVALID_ENDPOINT')
        require(re.fullmatch(r'[a-z0-9][a-z0-9.-]+[a-z0-9]', host) and '.' in host
                and not host.endswith(('.localhost', '.local', '.invalid', '.test', '.internal')),
                'INVALID_ENDPOINT')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('LITERAL_IP_NOT_LIVE_PROVIDER')
        require(len(url) <= 2048 and len(key) <= 8192 and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', model)
                and thinking in {'default', 'enabled', 'disabled'}, 'INVALID_CONFIG')
    except (ValueError, TypeError):
        return None, 'MODEL_CONFIGURATION_INVALID'
    return {'base_url': url, 'api_key': key, 'model': model, 'thinking_mode': thinking}, None


def child_environment(directory):
    # No copied ambient DB/auth/ERP/tracing/proxy credentials, nor model key in argv/env.
    return {'PATH': os.defpath, 'HOME': directory, 'TMPDIR': directory,
        'PYTHONPATH': str(ROOT / 'services/api') + os.pathsep + str(ROOT),
        'PYTHONIOENCODING': 'utf-8', 'PF_DATA_DIR': directory,
        'PF_DATABASE_URL': 'sqlite:///' + str(Path(directory) / 'trial.sqlite3'),
        'PF_MODE': 'demo', 'PF_ERP_MODE': 'mock', 'PF_ALLOW_CANDIDATE_QUEUE': '1',
        'ERP_ALLOW_DRAFT_WRITES': 'false', 'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'}


def execute_case(case, config):
    with tempfile.TemporaryDirectory(prefix='pf-candidate-trial-') as directory:
        cfg = {**config, 'directory': directory}
        try:
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--_worker'],
                input=json.dumps({'case': case, 'config': cfg}), text=True, encoding='utf-8',
                env=child_environment(directory), cwd=directory, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=config['wall_seconds'] + 20, check=False)
            require(proc.returncode == 0 and len(proc.stdout) <= 64000, 'INVALID_WORKER_REPORT')
            result = json.loads(proc.stdout)
            require(result['id'] == case['id'] and result['mode'] == config['mode']
                    and result['source_sha256'] == case['sha256'], 'WORKER_IDENTITY_MISMATCH')
            # Export whitelist rejects unexpected top-level content as well.
            require(set(result) == set(empty_record(case, config['mode'])), 'INVALID_WORKER_REPORT')
            return validate_record(result, case, config['mode'])
        except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError, OSError):
            result = empty_record(case, config['mode'], 'WORKER_TIMEOUT_OR_INVALID_REPORT')
            result.update(status='failed', provider_requests=None, network_attempted=None)
            return result


def run_suite(inputs, *, mode, allow_network=False, limit=12, max_requests=48, execute=execute_case):
    require(mode in {'baseline', 'fixture', 'live'} and type(limit) is int and 1 <= limit <= 12
            and type(max_requests) is int and 4 <= max_requests <= 48, 'INVALID_RUN_BUDGET')
    require(not allow_network or mode == 'live', 'NETWORK_FLAG_REQUIRES_LIVE_MODE')
    config = {'mode': mode, 'allow_network': allow_network, 'approved_host': '', 'max_calls': 4, 'wall_seconds': 45, 'token_budget': 16000,
        'base_url': 'https://fixture.invalid', 'api_key': 'synthetic-fixture', 'model': 'scripted-fixture',
        'thinking_mode': 'default'}
    stop = None
    if mode == 'live':
        provider, stop = live_config(allow_network)
        if provider:
            config.update(provider)
            config['approved_host'] = urlsplit(config['base_url']).hostname
    report = {'schema_version': 1, 'mode': mode, 'attempt': 1, 'status': 'running',
        'input_sha256': fingerprint(inputs), 'implementation_sha256': implementation_hash(),
        'source_scope': 'synthetic single-source extraction only', 'cases': [],
        'budgets': {'max_provider_requests': max_requests, 'per_case_calls': 4,
            'per_case_tools': 12, 'per_case_wall_seconds': 45, 'per_case_reported_tokens': 16000,
            'output_tokens_per_request': 1600, 'hard_currency_budget': False},
        'model_sha256': fingerprint(config['model']) if mode == 'live' and not stop else None,
        'endpoint_sha256': fingerprint(config['base_url']) if mode == 'live' and not stop else None,
        'usage_source': 'provider_reported_not_billing', 'cost': None}
    calls = 0
    for index, case in enumerate(inputs['cases']):
        if index >= limit:
            reason = 'OUTSIDE_EXPLICIT_CASE_LIMIT'
        elif stop:
            reason = stop
        elif mode != 'baseline' and calls + config['max_calls'] > max_requests:
            reason = 'INSUFFICIENT_REMAINING_REQUEST_BUDGET'
        else:
            reason = None
        if reason:
            record = empty_record(case, mode, reason)
        else:
            record = execute(case, config)
            consumed = record.get('provider_requests')
            if type(consumed) is int and 0 <= consumed <= config['max_calls']:
                calls += consumed
            else:
                stop = 'UNCERTAIN_PROVIDER_OUTCOME_NO_RETRY'
            # Never spend further calls after unknown accounting or provider failure.
            if mode == 'live' and (record['status'] != 'observed' or record.get('usage') is None):
                stop = 'PROVIDER_FAILURE_OR_USAGE_UNKNOWN_NO_RETRY'
        report['cases'].append(record)
    report['known_provider_requests'] = calls
    report['status'] = ('blocked' if all(x['status'] == 'blocked' for x in report['cases']) else
        'partial' if any(x['status'] == 'blocked' for x in report['cases']) else 'executed')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'run', 'grade', 'compare'))
    parser.add_argument('--suite', type=Path, default=DEFAULT_SUITE)
    parser.add_argument('--mode', choices=('baseline', 'fixture', 'live'), default='baseline')
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--max-requests', type=int, default=48)
    parser.add_argument('--observations', type=Path)
    parser.add_argument('--baseline-observations', type=Path)
    parser.add_argument('--require-all-success', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Reserve output BEFORE any credential read or network dispatch.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write('{"status":"interrupted_or_running","not_acceptance":true}\n'); stream.flush()
            inputs = load_inputs(args.suite)
            code = 0
            if args.command == 'run':
                report = run_suite(inputs, mode=args.mode, allow_network=args.allow_network,
                                   limit=args.limit, max_requests=args.max_requests)
                if report['status'] == 'blocked': code = 2
                elif report['status'] == 'partial' or (args.mode != 'baseline'
                        and any(c['status'] != 'observed' for c in report['cases'])): code = 1
            elif args.command == 'grade':
                require(args.observations is not None, 'OBSERVATIONS_REQUIRED')
                report = grade(args.suite, load_json(args.observations))
                if args.require_all_success and report['counts']['not_passed']: code = 1
            elif args.command == 'compare':
                require(args.observations is not None and args.baseline_observations is not None, 'BOTH_REPORTS_REQUIRED')
                report = compare(args.suite, load_json(args.baseline_observations), load_json(args.observations))
                if not report['real_model_acceptance']: code = 2
            else:
                report = {'status': 'passed', 'scope': 'input_integrity_only', 'tasks': len(inputs['cases']),
                          'input_sha256': fingerprint(inputs)}
            stream.seek(0); stream.truncate(); json.dump(report, stream, ensure_ascii=False, indent=2); stream.write('\n')
        print(json.dumps({'status': report['status'], 'exit_code': code,
            'counts': report.get('counts'), 'notice': 'No model-quality claim for baseline, fixture or blocked runs.'}))
        return code
    except (ValueError, OSError, TypeError, KeyError):
        print('{"status":"failed","reason":"INVALID_EVALUATION_INPUT_OR_EXISTING_OUTPUT"}')
        return 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--_worker']:
        # Suppress imported libraries/provider exceptions; only fixed sealed receipt exported.
        logging.disable(logging.CRITICAL)
        try:
            data = json.loads(sys.stdin.read(100000))
            cfg = data['config']
            require(cfg['mode'] in {'baseline', 'fixture', 'live'} and type(cfg['allow_network']) is bool
                    and type(cfg['max_calls']) is int and 1 <= cfg['max_calls'] <= 4
                    and type(cfg['wall_seconds']) is int and 1 <= cfg['wall_seconds'] <= 45
                    and type(cfg['token_budget']) is int and 1600 <= cfg['token_budget'] <= 16000, 'INVALID_WORKER_BUDGET')
            if cfg['mode'] == 'live':
                explicit = dict(LLM_BASE_URL=cfg['base_url'], LLM_API_KEY=cfg['api_key'], LLM_MODEL=cfg['model'],
                    PF_EVAL_APPROVED_HOST=cfg['approved_host'], LLM_THINKING_MODE=cfg['thinking_mode'])
                provider, reason = live_config(cfg['allow_network'], explicit)
                require(provider is not None and reason is None, 'INVALID_WORKER_OPT_IN')
            else:
                require(cfg['allow_network'] is False, 'INVALID_OFFLINE_OPT_IN')
                cfg.update(base_url='https://fixture.invalid', api_key='synthetic-fixture',
                           model='scripted-fixture', thinking_mode='default')
            # Always create a NEW directory, including direct --_worker invocations.
            with tempfile.TemporaryDirectory(prefix='pf-candidate-child-', dir=cfg['directory']) as fresh:
                cfg['directory'] = fresh
                os.environ.clear(); os.environ.update(child_environment(fresh))
                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    from evals.candidate_trial import run
                    result = run(data['case'], cfg)
            print(json.dumps(result, ensure_ascii=False))
        except Exception:
            print('{"status":"failed","reason":"INVALID_WORKER_CONFIGURATION"}')
        raise SystemExit(0)
    raise SystemExit(main())
