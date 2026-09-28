"""Real HTTP smoke with a separate outbox worker and server restart (mock ERP only)."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]

def run() -> dict:
    with tempfile.TemporaryDirectory(prefix='pf-smoke-') as directory:
        work = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'PF_DATA_DIR': directory, 'PF_DATABASE_URL': f'sqlite:///{work / "http.sqlite3"}',
               'PYTHONPATH': str(ROOT / 'services/api'), 'PF_MODE': 'demo', 'PF_ERP_MODE': 'mock'}
        env.pop('PF_AUTH_TOKENS', None)
        env['ERP_ALLOW_DRAFT_WRITES'] = 'false'
        for key in ('ERP_API_KEY', 'ERP_API_SECRET', 'ERP_BASE_URL', 'ERP_COMPANY', 'LLM_API_KEY'):
            env.pop(key, None)
        base = f'http://127.0.0.1:{port}'
        log = (work / 'api.log').open('w')
        server = None
        def start():
            process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/start.py'), '--port', str(port)],
                                       cwd=ROOT, env=env, stdout=log, stderr=log)
            for _ in range(80):
                try:
                    if httpx.get(base + '/health', timeout=0.2).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            process.terminate()
            process.wait(timeout=5)
            raise RuntimeError('Server startup failed: ' + (work/'api.log').read_text())
        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        steps = []
        try:
            server = start()
            with httpx.Client(base_url=base, timeout=10) as client:
                def call(method, path, role='demo-buyer', status=200, **kwargs):
                    response = client.request(method, '/api/v1'+path, headers={'Authorization': 'Bearer '+role}, **kwargs)
                    assert response.status_code == status, (path, response.status_code, response.text)
                    return response.json()
                assert 'ProcureFlow' in client.get('/').text
                assert client.get('/assets/app.js').status_code == 200
                request = call('POST', '/requests', status=201, json={'title':'HTTP smoke','sku':'STAND-01',
                    'quantity':'20','budget':'30000.00','max_delivery_days':14})
                rid = request['id']
                for name in ['supplier-a.txt', 'supplier-b.csv', 'supplier-c.pdf']:
                    quote = call('POST', f'/requests/{rid}/documents', status=201,
                        files={'file':(name, (ROOT/'evals/fixtures'/name).read_bytes())})
                    call('POST', f'/quotes/{quote["id"]}/confirm', json={'expected_version':1,'acknowledge':True})
                proposals = call('POST', f'/requests/{rid}/analyze', json={})
                proposal = proposals['proposal']
                assert proposal['total'] == '24200.00', proposal
                assert proposal['quote_values']['supplier_id'] == 'SUP-C'
                snapshot = {'snapshot_hash':proposal['snapshot_hash']}
                call('POST', f'/requests/{rid}/approval', status=403, json=snapshot)
                call('POST', f'/requests/{rid}/approval', role='demo-approver', json=snapshot)
                operation = call('POST', f'/requests/{rid}/execute', status=202, json=snapshot)
                assert operation['status'] == 'PENDING'
                steps.extend(['real-http-create', 'three-source-import', 'manual-confirmations', 'deterministic-total-24200',
                              'buyer-cannot-approve', 'independent-approval', 'durable-outbox-reservation'])
                for _ in range(2):
                    worker = subprocess.run([sys.executable, '-m', 'procureflow.worker', '--once'], cwd=ROOT,
                                            env=env, capture_output=True, text=True, timeout=20)
                    assert worker.returncode == 0, worker.stdout+worker.stderr
                final = call('GET', f'/operations/{operation["id"]}')
                assert final['status'] == 'COMPLETED', final
                remote_id = final['remote_id']
                assert remote_id.startswith('MOCK-SQ-')
                steps.extend(['separate-worker-process', 'second-worker-drain-no-repeat'])
                stop(server)
                server = start()
                restored = call('GET', f'/requests/{rid}')
                assert restored['status'] == 'ERP_CREATED'
                replay = call('POST', f'/requests/{rid}/execute', status=202, json=snapshot)
                assert replay['id'] == operation['id'] and replay['remote_id'] == remote_id
                call('GET', f'/requests/{rid}', role='demo-other-tenant', status=404)
                import sqlite3
                with sqlite3.connect(work/'mock-erp.sqlite3') as remote:
                    names = [r[0] for r in remote.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    assert 'drafts' in names, names
                    count = remote.execute('SELECT count(*) FROM drafts').fetchone()[0]
                assert count == 1, count
                steps.extend(['server-process-restart', 'persistent-request-and-operation', 'idempotent-replay',
                              'cross-tenant-denied', 'exactly-one-mock-draft-in-this-scenario'])
                return {'status':'passed','steps':steps,'supplier':'SUP-C','total_cny':'24200.00',
                        'remote_id':remote_id,'mock_erp_draft_count':count,'live_erp':False,'live_llm':False,
                        'browser_test':False,'restart_test':'graceful API process restart and separate worker; not SIGKILL chaos'}
        finally:
            if server is not None and server.poll() is None:
                stop(server)
            log.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run()
    text = json.dumps(result, ensure_ascii=False, indent=2)+'\n'
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
