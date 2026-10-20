"""Opt-in local protocol-fixture gate: real API and separate candidate Worker.

No paid model or ERP is contacted. All source data and identities are synthetic.
SIGKILL is real; lease expiry is explicitly injected in the disposable database.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
STEPS = ['enqueue_has_no_provider_call', 'queued_survives_api_restart',
         'separate_worker_extracts_and_replay_does_not_call', 'human_import_is_unconfirmed',
         'cancel_before_claim_makes_no_call', 'killed_inflight_is_interrupted_without_replay',
         'zero_approval_or_erp_operations']
VALUES = dict(supplier_id='Fixture Supplier', sku='QUEUE-01', quantity='20', uom='EA',
              unit_price='100.00', tax_mode='excluded', tax_rate='0', shipping_cost='0.00',
              discount='0.00', delivery_days=7, currency='CNY')
TEXT = '\n'.join(f'{key}: {value}' for key, value in VALUES.items())


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


@contextmanager
def provider_fixture():
    state = {'calls': 0, 'hold': False, 'entered': threading.Event(), 'release': threading.Event()}
    lock = threading.Lock()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            if self.path != '/chat/completions':
                self.send_error(404); return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 200000:
                self.send_error(413); return
            body = json.loads(self.rfile.read(length))
            with lock:
                state['calls'] += 1
                call_id = state['calls']
            if state['hold']:
                state['entered'].set()
                state['release'].wait(timeout=20)
            tools = [x for x in body['messages'] if x['role'] == 'tool']
            if not tools:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': f'fixture-{call_id}-{i}', 'type': 'function',
                     'function': {'name': name, 'arguments': json.dumps(arguments)}}
                    for i, (name, arguments) in enumerate([
                        ('get_request', {}), ('get_evidence', {'start': 0, 'limit': 24})])]}
                finish = 'tool_calls'
            else:
                fragments = next(json.loads(x['content'])['data']['fragments'] for x in tools
                                 if 'fragments' in json.loads(x['content']).get('data', {}))
                refs = [x['id'] for x in fragments[:1]]
                # Deliberately scripted response. Not a semantic extraction benchmark.
                result = {'summary': 'Synthetic fixture candidates require human review.',
                          'values': VALUES, 'references': {key: refs for key in VALUES},
                          'context_evidence_ids': refs, 'clarifications': []}
                message = {'role': 'assistant', 'content': json.dumps(result)}
                finish = 'stop'
            payload = json.dumps({'choices': [{'index': 0, 'finish_reason': finish, 'message': message}],
                                  'usage': {'prompt_tokens': 100, 'completion_tokens': 60, 'total_tokens': 160}}).encode()
            try:
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the intentionally killed Worker drops its socket.
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        state['release'].set(); server.shutdown(); server.server_close(); thread.join(timeout=5)


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


def exercise():
    report = {'status': 'running', 'scope': 'local HTTP protocol fixture with independent processes',
              'real_model_used': False, 'real_erp_used': False, 'synthetic_only': True,
              'lease_expiry_injected': True, 'steps': []}
    with tempfile.TemporaryDirectory(prefix='pf-candidate-worker-') as tmp, provider_fixture() as (provider, state):
        directory = Path(tmp); db = directory / 'business.sqlite3'
        token = secrets.token_hex(32)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('PF_', 'ERP_', 'LLM_', 'LANGSMITH_', 'LANGCHAIN_'))}
        env.update(PYTHONPATH=str(ROOT / 'services/api'), PF_MODE='private', PF_DATA_DIR=tmp,
                   PF_DATABASE_URL=f'sqlite:///{db}', PF_ERP_MODE='mock', PF_ALLOW_CANDIDATE_QUEUE='1',
                   PF_AUTH_TOKENS=json.dumps({token: {'tenant_id': 'fixture', 'user_id': 'buyer', 'role': 'buyer'}}))
        worker_env = {**env, 'PF_ALLOW_CANDIDATE_MODEL': '1', 'LLM_BASE_URL': provider,
                      'LLM_API_KEY': secrets.token_hex(32), 'LLM_MODEL': 'scripted-candidate-fixture'}
        migrated = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT / 'services/api',
                                  env=env, capture_output=True, timeout=45)
        require(migrated.returncode == 0, 'MIGRATION_FAILED')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        log = (directory / 'process.log').open('w')
        api_process = worker_process = None
        def start_api():
            proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts/start.py'), '--port', str(port)],
                                    cwd=ROOT, env=env, stdout=log, stderr=log)
            for _ in range(150):
                try:
                    if httpx.get(base + '/ready', timeout=1, trust_env=False).status_code == 200:
                        return proc
                except httpx.HTTPError: pass
                if proc.poll() is not None: break
                time.sleep(0.1)
            stop(proc); raise RuntimeError('API_NOT_READY')
        def worker_once():
            result = subprocess.run([sys.executable, '-m', 'procureflow.candidate_worker', '--once'],
                                    cwd=ROOT, env=worker_env, stdout=log, stderr=log, timeout=45)
            require(result.returncode == 0, 'WORKER_FAILED')
        try:
            api_process = start_api()
            with httpx.Client(base_url=base + '/api/v1', headers={'Authorization': 'Bearer ' + token},
                              timeout=20, trust_env=False) as client:
                def call(method, path, expected=200, **kwargs):
                    response = client.request(method, path, **kwargs)
                    require(response.status_code == expected, 'UNEXPECTED_API_STATUS')
                    return response.json()
                def prepare(label):
                    req = call('POST', '/requests', expected=201, json={'title': 'Synthetic ' + label,
                        'sku': 'QUEUE-01', 'quantity': '20', 'budget': '3000.00', 'max_delivery_days': 14})
                    doc = call('POST', f"/requests/{req['id']}/candidate-sources", expected=201,
                               files={'file': ('source.txt', TEXT.encode(), 'text/plain')})
                    run = call('POST', f"/requests/{req['id']}/candidate-runs", expected=201,
                               json={'document_id': doc['id'], 'expected_version': req['version'],
                                     'idempotency_key': secrets.token_hex(16)})
                    queued = call('POST', f"/candidate-runs/{run['id']}/enqueue", expected=202,
                                  json={'expected_revision': run['revision'], 'acknowledge_external_model': True})
                    require(queued['status'] == 'QUEUED', 'NOT_QUEUED')
                    return req, queued
                req, run = prepare('normal')
                require(state['calls'] == 0, 'API_CALLED_PROVIDER')
                report['steps'].append(STEPS[0])
                stop(api_process); api_process = start_api()
                require(call('GET', f"/candidate-runs/{run['id']}")['status'] == 'QUEUED', 'QUEUE_NOT_DURABLE')
                report['steps'].append(STEPS[1])
                worker_once()
                done = call('GET', f"/candidate-runs/{run['id']}")
                require(done['status'] == 'NEEDS_REVIEW' and done['output']['model_calls'] == 2, 'CANDIDATE_NOT_READY')
                require(state['calls'] == 2, 'UNEXPECTED_PROVIDER_COUNT')
                worker_once(); require(state['calls'] == 2, 'TERMINAL_REPLAYED')
                report['steps'].append(STEPS[2])
                quote = call('POST', f"/candidate-runs/{run['id']}/import", json={
                    'expected_revision': done['revision'], 'acknowledge_unverified_candidates': True,
                    'reason': 'Fixture reviewer reviewed the synthetic evidence; not a live human study.'})
                require(quote['confirmed_by'] is None, 'AUTO_CONFIRMED')
                report['steps'].append(STEPS[3])
                _, cancelled = prepare('cancel')
                call('POST', f"/candidate-runs/{cancelled['id']}/cancel", json={'expected_revision': cancelled['revision']})
                worker_once(); require(state['calls'] == 2, 'CANCELLED_CALLED_PROVIDER')
                report['steps'].append(STEPS[4])
                _, killed = prepare('kill')
                state['hold'] = True
                worker_process = subprocess.Popen([sys.executable, '-m', 'procureflow.candidate_worker', '--once'],
                    cwd=ROOT, env=worker_env, stdout=log, stderr=log)
                require(state['entered'].wait(timeout=20), 'PROVIDER_NOT_ENTERED')
                worker_process.kill(); worker_process.wait(timeout=5)
                require(worker_process.returncode == -signal.SIGKILL, 'KILL_NOT_OBSERVED')
                state['release'].set()
                require(call('GET', f"/candidate-runs/{killed['id']}")['status'] == 'RUNNING', 'INFLIGHT_NOT_DURABLE')
                # Do not wait 90s or pretend real time has elapsed: inject expiry only in this disposable DB.
                with sqlite3.connect(db) as connection:
                    connection.execute("UPDATE quote_candidate_runs SET lease_until=? WHERE id=?",
                                       ('2000-01-01T00:00:00+00:00', killed['id']))
                before = state['calls']; worker_once()
                require(call('GET', f"/candidate-runs/{killed['id']}")['status'] == 'INTERRUPTED', 'KILL_NOT_RECONCILED')
                require(state['calls'] == before == 3, 'UNCERTAIN_CALL_REPLAYED')
                report['steps'].append(STEPS[5])
                with sqlite3.connect(db) as connection:
                    counts = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                              for table in ('approvals', 'external_operations')}
                require(all(value == 0 for value in counts.values()), 'UNEXPECTED_BUSINESS_AUTHORITY')
                report['steps'].append(STEPS[6])
                report.update(status='passed', provider_http_requests=state['calls'], approval_count=0,
                              erp_operation_count=0, actual_sigkill_observed=True, api_had_provider_key=False)
        finally:
            stop(worker_process); stop(api_process); log.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    # Never silently replace a previous measurement.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        report = {'status': 'blocked', 'network_attempted': False, 'reason': 'EXPLICIT_FIXTURE_OPT_IN_REQUIRED'}
        if args.fixture:
            try: report = exercise()
            except Exception as error:
                codes = {'MIGRATION_FAILED', 'API_NOT_READY', 'WORKER_FAILED', 'UNEXPECTED_API_STATUS',
                         'NOT_QUEUED', 'API_CALLED_PROVIDER', 'QUEUE_NOT_DURABLE', 'CANDIDATE_NOT_READY',
                         'UNEXPECTED_PROVIDER_COUNT', 'TERMINAL_REPLAYED', 'AUTO_CONFIRMED',
                         'CANCELLED_CALLED_PROVIDER', 'PROVIDER_NOT_ENTERED', 'KILL_NOT_OBSERVED',
                         'INFLIGHT_NOT_DURABLE', 'KILL_NOT_RECONCILED', 'UNCERTAIN_CALL_REPLAYED',
                         'UNEXPECTED_BUSINESS_AUTHORITY'}
                report = {'status': 'failed', 'reason': str(error) if str(error) in codes else 'FIXTURE_EXECUTION_FAILED',
                          'real_model_used': False, 'real_erp_used': False}
        json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' and report['steps'] == STEPS else 2 if report['status'] == 'blocked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
