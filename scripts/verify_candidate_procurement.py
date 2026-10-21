"""Native candidate -> independent approval -> real ERP, in a fresh PG lab only.

The model endpoint is SCRIPTED, not an LLM or quality measurement. No upstream
model URL or key is accepted. Real ERP credentials come only from the existing
nonce-bound disposable sandbox; logs contain neither credentials nor page HTML.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'services/api'), str(ROOT/'apps/web/e2e')]
from verify_pilot_combined import isolated_environment, free_port, run_checked, start, stop, WEB
from erp_business_database import business_database, postgres_test_url
from verify_erp_sandbox import validate_lab, fault_proxy
from check_candidate_procurement import require, STEPS
from fixtures.candidate_provider import KEY, MODEL, VALUES, response_for, unique_object


@contextmanager
def provider(data):
    """Loopback HTTP protocol fixture; never forwards traffic or keeps raw requests."""
    require(data['company'] == 'ProcureFlow Sandbox' and data['sku'] == 'PF-SANDBOX-ITEM'
            and data['supplier'] == 'PF Synthetic Supplier', 'SYNTHETIC_DATA_REQUIRED')
    values = {**VALUES, 'supplier_id': data['supplier'], 'sku': data['sku']}
    source = '\n'.join(f'{k}: {v}' for k, v in values.items())
    state = {'calls': 0}
    lock = threading.Lock()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            if self.path != '/chat/completions' or self.headers.get('Authorization') != 'Bearer '+KEY:
                self.send_error(403); return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                require(0 < size <= 200000, 'FIXTURE_SIZE')
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(size), object_pairs_hook=unique_object)
                with lock:
                    require(state['calls'] < 6, 'FIXTURE_CALL_LIMIT')
                    result = response_for(body, state['calls'])
                    state['calls'] += 1
                message = result['choices'][0]['message']
                if message.get('content'):
                    answer = json.loads(message['content']); answer['values'] = values
                    message['content'] = json.dumps(answer, ensure_ascii=False)
                raw = json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
            except (ValueError, KeyError, TypeError, OSError):
                self.send_error(400)
    server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', source, state
    finally: server.shutdown(); server.server_close(); thread.join(timeout=5)


def audit_pg(db, operations, cancelled):
    from sqlalchemy import text
    with db.engine.connect() as conn:
        rows = conn.execute(text('SELECT id,status,quote_id,output FROM quote_candidate_runs')).mappings().all()
        require(len(rows)==4 and sum(r['status']=='IMPORTED' for r in rows)==3
                and sum(r['status']=='CANCELLED' for r in rows)==1, 'CANDIDATE_COUNTS')
        require(any(r['id']==cancelled and r['output'] is None for r in rows), 'CANCELLED_OUTPUT')
        quotes = conn.scalar(text('SELECT count(*) FROM quotes'))
        versions = conn.execute(text('SELECT confirmed_by,evidence FROM quote_versions')).mappings().all()
        require(quotes==3 and len(versions)==3 and all(r['confirmed_by'] for r in versions), 'QUOTE_COUNTS')
        for version in versions:
            require(all(ref.get('candidate_run_id') in {r['id'] for r in rows} for ref in version['evidence'].values()), 'CANDIDATE_PROVENANCE')
        actual = conn.execute(text('SELECT id,status,remote_id,attempts,approval_id FROM external_operations')).mappings().all()
        require(len(actual)==3, 'OPERATION_COUNTS')
        for expected in operations:
            row, = [r for r in actual if r['id']==expected['operation_id']]
            require(row['status']==expected['final_status'] and row['remote_id']==expected['remote_id'], 'OPERATION_MISMATCH')
            if expected['scenario']=='revoked': require(row['attempts']==0, 'REVOKED_DISPATCH')
        require(conn.scalar(text('SELECT count(*) FROM approvals'))==3, 'APPROVAL_COUNTS')
        require(conn.scalar(text("SELECT count(*) FROM outbox WHERE status='DONE'"))==3, 'OUTBOX_COUNTS')
        require(conn.scalar(text("SELECT count(*) FROM audit_events WHERE type='ERP_VERIFICATION_VERIFIED'"))==2, 'RECEIPT_COUNTS')
        require(str(conn.scalar(text('SELECT current_schema()'))).startswith('pf_erp_test_'), 'PG_SCHEMA')
        return {'status':'passed','database':'postgresql','candidate_runs':4,'imported':3,'cancelled':1,
            'quotes':3,'confirmed_quotes':3,'approvals':3,'operations':3,'completed':2,'denied':1,
            'done_outbox':3,'verified_receipts':2,'candidate_provenance_checked':True,
            'after_api_restart':True,'read_only_audit':True}


def exercise(data):
    import httpx
    from playwright.sync_api import sync_playwright, expect
    from pilot_combined import login, response_for as post_ui
    from procureflow.config import Settings
    from procureflow.db import Database
    from procureflow.auth import IdentityService
    from unittest.mock import patch
    report = {'schema':'candidate-procurement-v1','status':'running','steps':[], 'operations':[],
        'synthetic_only':True,'real_model_used':False,'scripted_provider':True,'live_erp':True,
        'browser_responses_not_mocked':True,'database':'postgresql','human_approval_measured':False,
        'source_tree':subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=ROOT,text=True).strip()}
    phase = 'scope'
    with tempfile.TemporaryDirectory(prefix='pf-candidate-erp-') as directory, \
            business_database('postgresql',directory) as database_url, fault_proxy() as (erp_url,wire), \
            provider(data) as (provider_url, source, state):
        api = web = db = None
        try:
            api_port,web_port=free_port(),free_port()
            while api_port==web_port: web_port=free_port()
            api_url,web_url=f'http://127.0.0.1:{api_port}',f'http://127.0.0.1:{web_port}'
            env=isolated_environment(directory,database_url,api_url,web_url,data,erp_url,False)
            env['PF_ALLOW_CANDIDATE_QUEUE']='1'
            run_checked([sys.executable,'-m','alembic','upgrade','head'],env,ROOT/'services/api')
            with patch.dict(os.environ,env,clear=True):
                settings=Settings(data_dir=Path(directory),database_url=database_url,mode='pilot',auth_tokens={},erp_mode='mock',erp_allow_draft_writes=False)
            db=Database(database_url); identities=IdentityService(db,settings);identities.create_tenant('lab')
            # Candidate Worker has model-fixture access but no ERP credentials/port.
            candidate_env={k:v for k,v in env.items() if not k.startswith(('ERP_','LLM_'))}
            candidate_env.update(PF_ERP_MODE='mock',ERP_ALLOW_DRAFT_WRITES='false',PF_ALLOW_CANDIDATE_MODEL='1',
                LLM_BASE_URL=provider_url,LLM_MODEL=MODEL,LLM_API_KEY=KEY)
            node_env={k:v for k,v in env.items() if not k.startswith(('ERP_','LLM_')) and k not in {'PF_DATABASE_URL','PF_DATA_DIR'}}
            phase='native-build';run_checked(['npm','run','build'],node_env,WEB,timeout=300)
            api_args=[sys.executable,str(ROOT/'scripts/start.py'),'--port',str(api_port)]
            api=start(api_args,env,api_url+'/ready')
            web=start(['node',str(WEB/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port',str(web_port)],node_env,web_url,WEB)
            require(httpx.get(api_url+'/ready',trust_env=False).json()['database']=='postgresql','PG_REQUIRED')
            require(not env.get('LLM_API_KEY') and not node_env.get('LLM_API_KEY'),'API_MODEL_SECRET')
            report['steps'].append(STEPS[0])
            def call(token,method,path,body=None,expected=200):
                r=httpx.request(method,api_url+'/api/v1'+path,json=body,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30)
                require(r.status_code==expected,'BUSINESS_HTTP_STATUS'); return r.json()
            def candidate_worker(): run_checked([sys.executable,'-m','procureflow.candidate_worker','--once'],candidate_env,timeout=90)
            def worker(): run_checked([sys.executable,'-m','procureflow.worker','--once'],env,timeout=90)
            def restart():
                nonlocal api
                stop(api);api=start(api_args,env,api_url+'/ready')
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                try:
                    for index,scenario in enumerate(('normal','lost-receipt','revoked','cancelled')):
                        phase=scenario; contexts=[]; before=len(wire['posts']);calls_before=state['calls']
                        try:
                            pages={};sessions={};users={role:f'joint-{index}-{role}' for role in ('buyer','approver')}
                            for role in users:
                                identities.set_membership('lab',users[role],role)
                                invite=identities.issue_invite('lab',users[role])['credential']
                                context=browser.new_context(viewport={'width':1440,'height':1100});contexts.append(context)
                                pages[role]=context.new_page();sessions[role]=login(pages[role],web_url,invite,role)
                            buyer,approver=pages['buyer'],pages['approver'];token=sessions['buyer']['token'];observer=sessions['approver']['token']
                            title='Synthetic candidate procurement '+scenario
                            buyer.get_by_role('button',name='＋ 新建采购需求',exact=True).click()
                            form=buyer.get_by_test_id('request-form')
                            for field,value in {'title':title,'sku':data['sku'],'quantity':'20','budget':'3000.00','max_delivery_days':'14'}.items():
                                form.locator(f'[name="{field}"]').fill(value)
                            req=post_ui(buyer,'/requests',lambda:form.get_by_role('button',name='保存需求').click(),201)
                            expect(buyer.locator('.request-panel').get_by_role('heading',name=title,exact=True)).to_be_visible()
                            panel=buyer.get_by_test_id('candidate-panel');upload=panel.get_by_role('button',name='上传候选原文',exact=True)
                            expect(upload).to_be_enabled()
                            with buyer.expect_file_chooser() as chosen: upload.click()
                            run=post_ui(buyer,f"/requests/{req['id']}/candidate-runs",lambda:chosen.value.set_files(
                                {'name':scenario+'.txt','mimeType':'text/plain','buffer':source.encode()}),201)
                            card=buyer.get_by_test_id('candidate-run').filter(has_text=run['id'])
                            panel.get_by_label('我允许将本任务原文发送给服务端配置的模型；可能产生费用').check()
                            post_ui(buyer,'/enqueue',lambda:card.get_by_role('button',name='开始候选提取').click(),202)
                            require(state['calls']==calls_before,'API_CALLED_PROVIDER')
                            if scenario=='cancelled':
                                post_ui(buyer,'/cancel',lambda:card.get_by_role('button',name='取消候选任务').click())
                                candidate_worker();require(state['calls']==calls_before,'CANCELLED_CALLED_PROVIDER')
                                cancelled=run['id'];report['steps'].append(STEPS[7]);continue
                            if scenario=='normal':
                                restart();require(call(token,'GET','/candidate-runs/'+run['id'])['status']=='QUEUED','QUEUE_NOT_DURABLE')
                            candidate_worker();expect(card).to_contain_text('等待人工核对',timeout=15000)
                            require(state['calls']==calls_before+2 and len(wire['posts'])==before,'CANDIDATE_SIDE_EFFECT')
                            card.get_by_label('我理解这不是已确认事实，后续仍须逐字段核对').check()
                            card.get_by_label('候选采纳理由').fill('Synthetic gate: human-role fixture checked every source field.')
                            quote=post_ui(buyer,'/import',lambda:card.get_by_role('button',name='导入为未确认报价').click())
                            require(quote['confirmed_by'] is None,'AUTO_CONFIRMED')
                            expect(buyer.get_by_test_id('quote-row')).to_have_count(1)
                            early=post_ui(buyer,'/analyze',lambda:buyer.get_by_test_id('analyze').click())
                            require(early['proposal'] is None,'UNCONFIRMED_PROPOSAL')
                            buyer.get_by_test_id('confirm-quote').click();buyer.get_by_test_id('ack-confirm').check()
                            post_ui(buyer,'/confirm',lambda:buyer.get_by_test_id('submit-confirm').click())
                            result=post_ui(buyer,'/analyze',lambda:buyer.get_by_test_id('analyze').click())
                            proposal=result['proposal'];require(proposal['total']=='2000.00' and result['llm_used'] is False,'COMPARISON_MISMATCH')
                            call(token,'POST','/requests/'+req['id']+'/execute',{'snapshot_hash':proposal['snapshot_hash']},409)
                            call(token,'POST','/requests/'+req['id']+'/approval',{'snapshot_hash':proposal['snapshot_hash']},403)
                            # Fresh independent login loads the newly created request; no role switching.
                            observer=login(approver,web_url,identities.issue_invite('lab',users['approver'])['credential'],'approver')['token']
                            approver.locator('#request-list').get_by_role('button').filter(has_text=title).click()
                            expect(approver.get_by_test_id('proposal-body')).to_contain_text('2000.00')
                            post_ui(approver,'/approval',lambda:approver.get_by_test_id('approve').click())
                            buyer.get_by_role('button',name='刷新状态',exact=True).click();expect(buyer.get_by_test_id('execute')).to_be_enabled()
                            op=post_ui(buyer,'/execute',lambda:buyer.get_by_test_id('execute').click(),202)
                            if scenario=='revoked': identities.set_membership('lab',users['approver'],'approver',active=False)
                            wire['lose_next']=scenario=='lost-receipt';worker()
                            observer=token if scenario=='revoked' else observer
                            first=call(observer,'GET','/operations/'+op['id'])
                            if scenario=='lost-receipt':
                                require(first['status']=='RECONCILING','RECOVERY_NOT_ENTERED');restart()
                                offset=len(wire['requests']);worker()
                                require(wire['requests'][offset:] and all(x['method']=='GET' for x in wire['requests'][offset:]),'RECOVERY_WROTE')
                            final=call(observer,'GET','/operations/'+op['id'])
                            if scenario=='revoked':
                                require(final['status']=='NEEDS_HUMAN' and final['error']=='APPROVER_REVOKED' and final['remote_id'] is None,'REVOKED_NOT_BLOCKED')
                                require(len(wire['posts'])==before,'REVOKED_ERP_WRITE');report['steps'].append(STEPS[6])
                            else:
                                require(final['status']=='COMPLETED' and not final['remote_id'].startswith('MOCK-'),'REAL_DRAFT_REQUIRED')
                                receipt=call(observer,'POST','/operations/'+op['id']+'/verify')
                                require(receipt['status']=='verified' and receipt['simulated'] is False,'READBACK_FAILED')
                                call(token,'POST','/requests/'+req['id']+'/execute',{'snapshot_hash':proposal['snapshot_hash']},202)
                                worker();require(len(wire['posts'])==before+1,'DUPLICATE_ERP_WRITE')
                                report['steps'].extend(STEPS[1:5] if scenario=='normal' else [STEPS[5]])
                            report['operations'].append({'scenario':scenario,'candidate_run_id':run['id'],'operation_id':op['id'],
                                'snapshot_hash':proposal['snapshot_hash'],'remote_id':final['remote_id'],'final_status':final['status'],
                                'expected_total':'2000.00'})
                        finally:
                            for ctx in contexts: ctx.close()
                finally:browser.close()
            restart();report['business_audit']=audit_pg(db,report['operations'],cancelled)
            report['steps'].append(STEPS[8]);report.update(status='passed',provider_requests=state['calls'],erp_post_attempts=len(wire['posts']),
                api_without_model_key=True,candidate_worker_without_erp_key=True)
        except Exception as exc:
            report.update(status='failed',phase=phase,reason='CANDIDATE_PROCUREMENT_FAILED',
                frames=[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)[-6:]])
        finally:
            stop(web);stop(api)
            if db:
                db.engine.dispose()
                if db.fence_engine is not None:db.fence_engine.dispose()
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ephemeral-test',action='store_true');p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    try:
        require(args.ephemeral_test and not sys.flags.optimize,'EXPLICIT_EPHEMERAL_TEST_REQUIRED')
        postgres_test_url();data=validate_lab('.data/erp-sandbox.json','.env.erp-sandbox')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x') as stream:
            stream.write('{"status":"interrupted_or_running"}\n');stream.flush()
            result=exercise(data);stream.seek(0);stream.truncate();json.dump(result,stream,indent=2)
        return 0 if result['status']=='passed' else 1
    except (ValueError,OSError,KeyError):
        print('{"status":"blocked","reason":"LAB_CONFIGURATION_OR_OUTPUT_INVALID"}');return 2

if __name__=='__main__':raise SystemExit(main())
