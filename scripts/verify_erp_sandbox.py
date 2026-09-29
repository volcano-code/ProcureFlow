"""Exercise real HTTP/API/Worker/ERPNext in a NEW local disposable lab only.

Not a general live-account write probe. Requires matching random lab markers;
creates only synthetic requests, field confirmations and simulated human-role
approvals. No real human procurement approval or model quality is asserted.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote
import httpx

ROOT=Path(__file__).resolve().parents[1]
TARGET='http://127.0.0.1:18080'


def validate_lab(credentials, env_file):
    marker=dict(line.split('=',1) for line in Path(env_file).read_text().splitlines() if '=' in line)
    data=json.loads(Path(credentials).read_text())
    if (data.get('site') != 'pf-erp-test.local' or data.get('company') != 'ProcureFlow Sandbox'
            or data.get('user') != 'pf-integration@example.invalid' or data.get('sku') != 'PF-SANDBOX-ITEM'
            or len(data.get('nonce','')) != 64 or data['nonce'] != marker.get('PF_EPHEMERAL_NONCE')
            or not data.get('api_key') or not data.get('api_secret')):
        raise ValueError('LAB_CONFIGURATION_MISMATCH')
    return data


@contextmanager
def fault_proxy():
    """Loopback-only forwarding of test traffic; lose one committed POST receipt."""
    state={'posts':[], 'lose_next':False}
    upstream=httpx.Client(base_url=TARGET,trust_env=False,follow_redirects=False,timeout=30)
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def handle_request(self):
            path=unquote(self.path.split('?',1)[0])
            allowed=(path.startswith('/api/resource/') or path=='/api/method/frappe.auth.get_logged_user')
            if not allowed or '..' in path:
                self.send_error(404); return
            if self.command=='POST' and path!='/api/resource/Supplier Quotation':
                self.send_error(403); return
            length=int(self.headers.get('Content-Length','0'))
            if length>2*1024*1024: self.send_error(413); return
            body=self.rfile.read(length) if length else b''
            headers={k:self.headers[k] for k in ('Authorization','Content-Type','Accept') if k in self.headers}
            try:
                response=upstream.request(self.command,self.path,content=body,headers=headers)
                content=response.content; status=response.status_code
                if self.command=='POST':
                    key=json.loads(body).get('custom_procureflow_operation_key')
                    state['posts'].append({'key':key,'status':status})
                    if state['lose_next'] and status<300:
                        state['lose_next']=False; status=504
                        content=b'{"error":"INJECTED_RECEIPT_LOSS_AFTER_COMMIT"}'
                self.send_response(status); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(content))); self.end_headers(); self.wfile.write(content)
            except httpx.HTTPError:
                self.send_error(502)
        do_GET=handle_request
        do_POST=handle_request
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}',state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5);upstream.close()


def exercise(data):
    report={'scope':'disposable real ERPNext draft roundtrip','status':'running','synthetic_only':True,
        'real_user_account_used':False,'human_approval_measured':False,'model_used':False,'steps':[]}
    with tempfile.TemporaryDirectory(prefix='pf-real-erp-') as tmp, fault_proxy() as (erp_url,wire):
        tokens={role:secrets.token_hex(32) for role in ('buyer','approver','self','other','auditor')}
        identities={tokens[role]:{'tenant_id':'lab' if role!='other' else 'other','user_id':'buyer' if role=='self' else role,
            'role':'approver' if role=='self' else 'buyer' if role=='other' else role} for role in tokens}
        env={k:v for k,v in os.environ.items() if not k.startswith(('PF_','ERP_','LLM_'))}
        env.update(PYTHONPATH=str(ROOT/'services/api'),PF_DATA_DIR=tmp,PF_DATABASE_URL=f'sqlite:///{tmp}/business.sqlite3',
            PF_MODE='private',PF_AUTH_TOKENS=json.dumps(identities),PF_ERP_MODE='erpnext',ERP_BASE_URL=erp_url,
            ERP_COMPANY=data['company'],ERP_API_KEY=data['api_key'],ERP_API_SECRET=data['api_secret'],ERP_ALLOW_DRAFT_WRITES='true')
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        log=open(Path(tmp)/'api.log','w')
        def start():
            p=subprocess.Popen([sys.executable,str(ROOT/'scripts/start.py'),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            for _ in range(150):
                try:
                    if httpx.get(base+'/ready',timeout=1,trust_env=False).status_code==200:return p
                except httpx.HTTPError: pass
                if p.poll() is not None:break
                time.sleep(.1)
            p.terminate();p.wait(timeout=10);raise AssertionError('PROCUREFLOW_START_FAILED')
        def stop(p):
            p.terminate()
            try:p.wait(timeout=10)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        migrated=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],
            cwd=ROOT/'services/api',env=env,capture_output=True,timeout=60)
        if migrated.returncode != 0:
            log.close();raise AssertionError('PROCUREFLOW_MIGRATION_FAILED')
        p=start()
        try:
            with httpx.Client(base_url=base,trust_env=False,timeout=30) as client:
                def call(method,path,role='buyer',expected=200,**kwargs):
                    r=client.request(method,'/api/v1'+path,headers={'Authorization':'Bearer '+tokens[role]},**kwargs)
                    if r.status_code!=expected:raise AssertionError(f'HTTP_{r.status_code}_EXPECTED_{expected}')
                    return r.json()
                def prepare(title):
                    req=call('POST','/requests',expected=201,json={'title':title,'sku':data['sku'],'quantity':'20','budget':'3000.00','max_delivery_days':14})
                    values={'supplier_id':data['supplier'],'sku':data['sku'],'quantity':'20','uom':'EA','unit_price':'100.00',
                        'tax_mode':'excluded','tax_rate':'0','shipping_cost':'0','discount':'0','delivery_days':7,'currency':'CNY'}
                    text='\n'.join(f'{k}: {v}' for k,v in values.items()).encode()
                    q=call('POST',f"/requests/{req['id']}/documents",expected=201,files={'file':('synthetic.txt',text)})
                    call('POST',f"/quotes/{q['id']}/confirm",json={'expected_version':q['version'],'acknowledge':True})
                    proposal=call('POST',f"/requests/{req['id']}/analyze",json={})['proposal']
                    assert proposal and proposal['total']=='2000.00'
                    return req,proposal
                def approve(req,proposal):
                    call('POST',f"/requests/{req['id']}/approval",role='approver',json={'snapshot_hash':proposal['snapshot_hash']})
                def enqueue(req,proposal):
                    return call('POST',f"/requests/{req['id']}/execute",expected=202,json={'snapshot_hash':proposal['snapshot_hash']})
                def worker():
                    r=subprocess.run([sys.executable,'-m','procureflow.worker','--once'],cwd=ROOT,env=env,capture_output=True,timeout=90)
                    assert r.returncode==0,'WORKER_FAILED'
                # Dedicated ERP identity, not Administrator, before any business write.
                sys.path.insert(0,str(ROOT/'services/api'))
                from procureflow.erp import ERPNextClient
                adapter=ERPNextClient(erp_url,data['api_key'],data['api_secret'],data['company'])
                try:report['preflight']=adapter.preflight(expected_user=data['user'])
                finally:adapter.client.close()
                report['steps'].append('dedicated_identity_get_only_preflight')
                req,proposal=prepare('Synthetic approval safeguards')
                call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':proposal['snapshot_hash']})
                call('POST',f"/requests/{req['id']}/approval",role='self',expected=403,json={'snapshot_hash':proposal['snapshot_hash']})
                approve(req,proposal)
                current=call('GET',f"/requests/{req['id']}")
                call('PUT',f"/requests/{req['id']}",json={k:current[k] for k in ('title','sku','budget','max_delivery_days')}|
                    {'quantity':'21','expected_version':current['version']})
                call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':proposal['snapshot_hash']})
                assert wire['posts']==[]
                report['steps'].append('unapproved_self_approved_and_stale_writes_denied')
                verified=[]
                for label,lose in [('normal',False),('lost-receipt',True)]:
                    req,proposal=prepare('Synthetic '+label);approve(req,proposal);op=enqueue(req,proposal)
                    wire['lose_next']=lose;worker()
                    first=call('GET',f"/operations/{op['id']}")
                    if lose:
                        assert first['status']=='RECONCILING',first.get('error','RECOVERY_NOT_ENTERED')
                        worker()
                    final=call('GET',f"/operations/{op['id']}")
                    assert final['status']=='COMPLETED',final.get('error','NOT_COMPLETED')
                    assert not final['remote_id'].startswith('MOCK-')
                    receipt=call('POST',f"/operations/{op['id']}/verify",role='auditor')
                    assert receipt['status']=='verified' and receipt['simulated'] is False
                    assert not receipt['external_write_attempted']
                    call('POST',f"/operations/{op['id']}/verify",role='other',expected=404)
                    assert enqueue(req,proposal)['id']==op['id'];worker()
                    assert len([x for x in wire['posts'] if x['key']==op['id']])==1
                    verified.append({'operation_id':op['id'],'snapshot_hash':proposal['snapshot_hash'],
                        'remote_id':final['remote_id'],'expected_total':'2000.00','scenario':label})
                    report['steps'].append(label+'_independent_worker_draft_readback_and_replay')
                stop(p);p=start()
                for result in verified:
                    assert call('GET',f"/operations/{result['operation_id']}")['remote_id']==result['remote_id']
                report['steps'].append('api_process_restart_retains_remote_ids')
                report.update(status='passed',operations=verified,post_attempts=len(wire['posts']),
                    real_erpnext_drafts_verified=len(verified),api_business_database='SQLite',real_erp_database='MariaDB',
                    fault_injection='loopback HTTP gateway returns 504 after ERP commits; next worker only reads')
        finally:stop(p);log.close()
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ephemeral-test',action='store_true')
    p.add_argument('--credentials',type=Path,default=Path('.data/erp-sandbox.json'))
    p.add_argument('--env-file',type=Path,default=Path('.env.erp-sandbox'))
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    report={'status':'blocked','network_attempted':False,'reason':'EXPLICIT_EPHEMERAL_TEST_REQUIRED'};code=2
    if a.ephemeral_test:
        try:
            data=validate_lab(a.credentials,a.env_file)
        except (OSError,ValueError,KeyError,TypeError):
            report['reason']='LAB_CONFIGURATION_MISMATCH'
        else:
            try: report=exercise(data);code=0
            except Exception as error:
                reason=str(error)
                report={'status':'failed','network_attempted':True,'reason':reason if reason.replace('_','').isalnum() else type(error).__name__}
                code=1
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));return code

if __name__=='__main__':raise SystemExit(main())
