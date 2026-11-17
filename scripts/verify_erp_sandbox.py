"""Exercise real HTTP/API/Worker/ERPNext in a NEW local disposable lab only.

Not a general live-account write probe. Requires matching random lab markers;
creates only synthetic requests, field confirmations and simulated human-role
approvals. No real human procurement approval or model quality is asserted.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
import http.server
import json
import os
import re
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
sys.path.insert(0, str(ROOT / 'scripts'))
from erp_business_database import business_database, postgres_test_url, audit_business_database
sys.path.insert(0, str(ROOT / 'integrations/erpnext/sandbox'))
from cost_fixtures import ACCEPTANCE_CASES, TAX_ACCOUNT, FREIGHT_ACCOUNT, acceptance_cases
from erp_tabular_fixtures import expected_values, import_tabular_quote, save_source_files
from erp_multi_item_fixtures import import_multi_item_quote, save_source_file as save_multi_source
from multi_cost_audit import verify_multi_cost_document
from cost_fixtures import MULTI_ITEM_CASE
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



def remote_error_details(body):
    """Extract only allowlisted source locations and DocType names, never messages."""
    files = {'app.py', '__init__.py', 'v1.py', 'client.py', 'document.py', 'base_document.py',
        'permissions.py', 'db_query.py', 'query.py', 'builder.py', 'utils.py',
        'supplier_quotation.py', 'buying_controller.py', 'accounts_controller.py',
        'stock_controller.py', 'transaction_base.py', 'get_item_details.py',
        'taxes_and_totals.py', 'pricing_rule.py', 'party.py', 'item.py', 'item_price.py',
        'price_list.py', 'warehouse.py', 'stock_ledger.py'}
    doctypes = ('Supplier Quotation', 'Supplier', 'Company', 'Item Price', 'Item', 'UOM',
        'Currency', 'Custom Field', 'Price List', 'Account', 'Cost Center', 'Warehouse',
        'Item Tax Template', 'Purchase Taxes and Charges Template', 'Buying Settings',
        'Stock Settings', 'Supplier Group', 'Item Group', 'Currency Exchange', 'Pricing Rule')
    details = {}
    raw = body.get('exc', '')
    if isinstance(raw, str) and len(raw) <= 65536:
        try:
            traces = json.loads(raw)
        except ValueError:
            traces = [raw]
        if isinstance(traces, list) and all(isinstance(x, str) for x in traces):
            frames = []
            for filename, line in re.findall(r'File "[^"\n]*/([A-Za-z0-9_.-]+)", line ([0-9]{1,6})', '\n'.join(traces)):
                if filename in files:
                    frames.append({'file': filename, 'line': int(line)})
            if frames:
                details['source_locations'] = frames[-8:]
    messages = body.get('_server_messages', '')
    if isinstance(messages, str) and len(messages) <= 16384:
        mentioned = [dt for dt in doctypes if re.search(r'(?<![A-Za-z])' + re.escape(dt) + r'(?![A-Za-z])', messages)]
        if mentioned:
            details['mentioned_doctypes'] = mentioned
    return details


def http_observation(method, path, response):
    """Retain only fixed endpoint families, status and allowlisted error classes."""
    family = 'other'
    for resource in ('Custom Field', 'Company', 'Supplier Quotation', 'Supplier'):
        prefix = '/api/resource/' + resource
        if path == prefix or path.startswith(prefix + '/'):
            family = resource
            break
    if path == '/api/method/frappe.auth.get_logged_user':
        family = 'identity'
    result = {'method': method if method in {'GET', 'POST'} else 'other',
        'endpoint': family, 'status': response.status_code}
    try:
        body = response.json()
        kind = body.get('exc_type') if isinstance(body, dict) else None
        if kind in {'PermissionError', 'AuthenticationError', 'ValidationError',
                'LinkValidationError', 'MandatoryError', 'DoesNotExistError',
                'TypeError', 'ValueError', 'AttributeError', 'KeyError', 'ZeroDivisionError'}:
            result['error_type'] = kind
            result.update(remote_error_details(body))
    except (ValueError, TypeError):
        pass
    return result


def document_contract_observation(expected, response):
    """Compare fixed draft fields without exporting either payload or document."""
    try:
        record = response.json().get('data')
        if not isinstance(record, dict) or not isinstance(record.get('items'), list):
            return {'document_shape_valid': False}
        if len(expected.get('items', [])) == 2:
            try:
                verify_multi_cost_document(record, MULTI_ITEM_CASE)
                complete = True
            except (AssertionError, ValueError, TypeError, KeyError):
                complete = False
            return {'document_shape_valid': len(record['items']) == 2,
                'multi_item_cost_contract_matches': complete,
                **{field + '_matches': record.get(field) == expected.get(field) for field in
                   ('supplier', 'company', 'currency', 'transaction_date',
                    'custom_procureflow_operation_key', 'custom_procureflow_snapshot_hash')}}
        if len(record['items']) != 1 or not isinstance(record['items'][0], dict):
            return {'document_shape_valid': False}
        item, wanted = record['items'][0], expected['items'][0]
        checks = {'document_shape_valid': True,
            'draft_status_matches': type(record.get('docstatus')) is int and record['docstatus'] == 0}
        for field in ('supplier', 'company', 'currency', 'transaction_date',
                      'custom_procureflow_operation_key', 'custom_procureflow_snapshot_hash'):
            checks[field + '_matches'] = record.get(field) == expected.get(field)
        for field in ('item_code', 'uom'):
            checks[field + '_matches'] = item.get(field) == wanted.get(field)
        for field in ('qty', 'rate'):
            checks[field + '_matches'] = Decimal(str(item.get(field))) == Decimal(str(wanted.get(field)))
        goods = (Decimal(str(wanted['qty'])) * Decimal(str(wanted['rate']))).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')
        discount = Decimal(str(expected.get('discount_amount', '0')))
        rows = expected.get('taxes', [])
        tax = sum((Decimal(str(row['rate'])) / 100 for row in rows
                   if row['charge_type'] == 'On Net Total' and not row['included_in_print_rate']), Decimal(0))
        freight = sum((Decimal(str(row['tax_amount'])) for row in rows if row['charge_type'] == 'Actual'), Decimal(0))
        target = goods - discount + ((goods - discount) * tax).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP') + freight
        checks['grand_total_matches'] = Decimal(str(record.get('grand_total'))) == target
        if 'taxes' in expected:
            actual_rows = record.get('taxes')
            checks['cost_rows_match'] = isinstance(actual_rows, list) and len(actual_rows) == len(rows)
            if checks['cost_rows_match']:
                for actual, want in zip(actual_rows, rows, strict=True):
                    checks['cost_rows_match'] &= all(actual.get(key) == want.get(key) for key in
                        ('charge_type', 'category', 'add_deduct_tax', 'account_head', 'included_in_print_rate'))
                    checks['cost_rows_match'] &= Decimal(str(0 if actual.get('rate') is None and actual.get('charge_type') == 'Actual' else actual.get('rate'))) == Decimal(str(want['rate']))
                    if want['charge_type'] == 'Actual':
                        checks['cost_rows_match'] &= Decimal(str(actual.get('tax_amount_after_discount_amount'))) == Decimal(str(want['tax_amount']))
            checks['discount_amount_matches'] = Decimal(str(record.get('discount_amount'))) == discount
            checks['apply_discount_on_matches'] = record.get('apply_discount_on') == expected.get('apply_discount_on')
        return checks
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        return {'document_shape_valid': False}


def safe_failure(error):
    reason = str(error)
    if re.fullmatch(r'ERP_HTTP_[0-9]{3}|HTTP_[0-9]{3}_EXPECTED_[0-9]{3}', reason):
        return reason
    if reason in {'PROCUREFLOW_START_FAILED', 'PROCUREFLOW_MIGRATION_FAILED', 'BUSINESS_DATABASE_BACKEND_MISMATCH', 'POSTGRES_BUSINESS_AUDIT_FAILED',
            'WORKER_FAILED', 'RECOVERY_NOT_ENTERED', 'NOT_COMPLETED',
            'ERP_READBACK_PAYLOAD_MISMATCH', 'ERP_RESULT_UNKNOWN', 'ERP_ADAPTER_FAILURE',
            'ERP_REMOTE_OPERATION_KEY_MISMATCH', 'REMOTE_PAYLOAD_MISMATCH'}:
        return reason
    return 'BUSINESS_ROUNDTRIP_FAILED'


@contextmanager
def fault_proxy():
    """Loopback-only forwarding of test traffic; lose one committed POST receipt."""
    state={'posts':[], 'lose_next':False, 'requests':[]}
    expected_draft = {}  # Ephemeral local comparison input; never included in reports.
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
                observation = http_observation(self.command, path, response)
                if self.command == 'POST' and response.status_code < 300:
                    expected_draft.clear()
                    expected_draft.update(json.loads(body))
                elif (self.command == 'GET' and path.startswith('/api/resource/Supplier Quotation/')
                      and expected_draft and response.status_code < 300):
                    observation['draft_contract'] = document_contract_observation(expected_draft, response)
                state['requests'] = (state['requests'] + [observation])[-64:]
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


def exercise(data, backend='sqlite', include_multi_item=False):
    cases = acceptance_cases(include_multi_item)
    report={'scope':'disposable real ERPNext draft roundtrip','status':'running','synthetic_only':True,
        'real_user_account_used':False,'human_approval_measured':False,'model_used':False,'steps':[]}
    if include_multi_item:
        report['include_multi_item'] = True
    with tempfile.TemporaryDirectory(prefix='pf-real-erp-') as tmp, business_database(backend, tmp) as database_url, fault_proxy() as (erp_url,wire):
        tokens={role:secrets.token_hex(32) for role in ('buyer','approver','self','other','auditor')}
        identities={tokens[role]:{'tenant_id':'lab' if role!='other' else 'other','user_id':'buyer' if role=='self' else role,
            'role':'approver' if role=='self' else 'buyer' if role=='other' else role} for role in tokens}
        env={k:v for k,v in os.environ.items() if not k.startswith(('PF_','ERP_','LLM_'))}
        env.update(PYTHONPATH=str(ROOT/'services/api'),PF_DATA_DIR=tmp,PF_DATABASE_URL=database_url,
            PF_MODE='private',PF_AUTH_TOKENS=json.dumps(identities),PF_ERP_MODE='erpnext',ERP_BASE_URL=erp_url,
            ERP_COMPANY=data['company'],ERP_API_KEY=data['api_key'],ERP_API_SECRET=data['api_secret'],ERP_ALLOW_DRAFT_WRITES='true',
            ERP_TAX_ACCOUNT=TAX_ACCOUNT, ERP_FREIGHT_ACCOUNT=FREIGHT_ACCOUNT)
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        log=open(Path(tmp)/'api.log','w')
        def start():
            p=subprocess.Popen([sys.executable,str(ROOT/'scripts/start.py'),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            for _ in range(150):
                try:
                    ready=httpx.get(base+'/ready',timeout=1,trust_env=False)
                    if ready.status_code==200:
                        if ready.json().get('database') != backend:
                            stop(p)
                            raise AssertionError('BUSINESS_DATABASE_BACKEND_MISMATCH')
                        return p
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
        phase='erp-preflight'
        try:
            with httpx.Client(base_url=base,trust_env=False,timeout=30) as client:
                def call(method,path,role='buyer',expected=200,**kwargs):
                    r=client.request(method,'/api/v1'+path,headers={'Authorization':'Bearer '+tokens[role]},**kwargs)
                    if r.status_code!=expected:raise AssertionError(f'HTTP_{r.status_code}_EXPECTED_{expected}')
                    return r.json()
                def restart():
                    nonlocal p
                    stop(p); p = start()
                def prepare(title, scenario='normal'):
                    fixture = cases[scenario]
                    req=call('POST','/requests',expected=201,json={'title':title,'budget':'30000.00','max_delivery_days':14,
                        **({'lines': [{key: line[key] for key in ('sku','quantity','uom')} for line in fixture['lines']]}
                           if fixture.get('multi_item') else {'sku':data['sku'],'quantity':'20'})})
                    if fixture.get('multi_item'):
                        before_posts = len(wire['posts'])
                        q, provenance = import_multi_item_quote(call, req['id'], data, restart)
                        assert len(wire['posts']) == before_posts
                    elif fixture['input_format'] == 'txt':
                        values = expected_values(data, scenario)
                        text='\n'.join(f'{k}: {v}' for k,v in values.items()).encode()
                        q=call('POST',f"/requests/{req['id']}/documents",expected=201,files={'file':('synthetic.txt',text)})
                        provenance = {'input_format': 'txt'}
                    else:
                        before_posts = len(wire['posts'])
                        q, provenance = import_tabular_quote(call, req['id'], data, scenario, restart)
                        assert len(wire['posts']) == before_posts
                    confirmed=call('POST',f"/quotes/{q['id']}/confirm",json={'expected_version':q['version'],'acknowledge':True})
                    assert confirmed['confirmed_by'] == 'buyer' and confirmed['values'] == q['values']
                    assert confirmed['evidence'] == q['evidence']
                    comparison=call('POST',f"/requests/{req['id']}/analyze",json={})
                    proposal=comparison['proposal']
                    assert comparison['llm_used'] is False and comparison['evaluation']['current']
                    assert proposal and proposal['total']==fixture['total'] and proposal['quote_id']==q['id']
                    return req,proposal,q,provenance
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
                adapter=ERPNextClient(erp_url,data['api_key'],data['api_secret'],data['company'], tax_account=TAX_ACCOUNT, freight_account=FREIGHT_ACCOUNT)
                try:report['preflight']=adapter.preflight(expected_user=data['user'])
                finally:adapter.client.close()
                report['steps'].append('dedicated_identity_get_only_preflight')
                phase='approval-safeguards'
                req,proposal,_,_=prepare('Synthetic approval safeguards')
                call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':proposal['snapshot_hash']})
                call('POST',f"/requests/{req['id']}/approval",role='self',expected=403,json={'snapshot_hash':proposal['snapshot_hash']})
                approve(req,proposal)
                current=call('GET',f"/requests/{req['id']}")
                call('PUT',f"/requests/{req['id']}",json={k:current[k] for k in ('title','sku','budget','max_delivery_days')}|
                    {'quantity':'21','expected_version':current['version']})
                call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':proposal['snapshot_hash']})
                assert wire['posts']==[]
                report['steps'].append('unapproved_self_approved_and_stale_writes_denied')
                phase='tabular-approval-safeguards'
                # Real table imports, then independent quote and policy invalidation.
                # These requests never enqueue or create an external operation.
                for scenario in ('csv-excluded-discount', 'xlsx-excluded-discount'):
                    req,proposal,quote,_=prepare('Synthetic stale table approval', scenario)
                    approve(req,proposal)
                    changed=call('PUT',f"/quotes/{quote['id']}",json={
                        'expected_version':quote['version'], 'values':{**quote['values'], 'shipping_cost':'81.00'},
                        'reason':'Synthetic corrected freight invalidates table approval'})
                    assert changed['confirmed_by'] is None and changed['version'] == quote['version'] + 1
                    call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':proposal['snapshot_hash']})
                    call('POST',f"/quotes/{changed['id']}/confirm",json={'expected_version':changed['version'],'acknowledge':True})
                    current=call('POST',f"/requests/{req['id']}/analyze",json={})['proposal']
                    assert current and current['snapshot_hash'] != proposal['snapshot_hash']
                    approve(req,current)
                    policy=call('GET','/policy')
                    call('POST','/policy/versions',role='approver',expected=201,json={
                        'expected_version':policy['latest_version'], 'budget_cap':policy['budget_cap'],
                        'max_delivery_days':policy['max_delivery_days'],
                        'minimum_valid_quotes':policy['minimum_valid_quotes'],
                        'reason':'Synthetic policy version invalidates prior table approval'})
                    call('POST',f"/requests/{req['id']}/execute",expected=409,json={'snapshot_hash':current['snapshot_hash']})
                    assert wire['posts'] == []
                report['steps'].append('tabular_preview_import_and_stale_quote_policy_writes_denied')
                verified=[]
                import_replays=[]
                for label, fixture in cases.items():
                    lose = fixture['lose_receipt']
                    phase=label
                    req,proposal,quote,provenance=prepare('Synthetic '+label, label);approve(req,proposal);op=enqueue(req,proposal)
                    wire['lose_next']=lose;worker()
                    first=call('GET',f"/operations/{op['id']}")
                    if lose:
                        assert first['status']=='RECONCILING',first.get('error','RECOVERY_NOT_ENTERED')
                        restart()
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
                    if fixture.get('multi_item'):
                        provenance.update(item_count=2, independent_readback_verified=True,
                            lost_receipt_reconciled=True, idempotent_replay_verified=True,
                            single_post_verified=True, source_rows_verified=True, shipping_count=1,
                            transaction_date=proposal['transaction_date'], supplier_id=proposal['quote_values']['supplier_id'],
                            contract_version=proposal['contract_version'], erp_cost_mapping_version=proposal['erp_cost_mapping_version'])
                    verified.append({'operation_id':op['id'],'snapshot_hash':proposal['snapshot_hash'],
                        'remote_id':final['remote_id'],'expected_total':fixture['total'],'scenario':label,
                        'cost_components_verified': True, **provenance})
                    if fixture['input_format'] != 'txt':
                        current_quote, = call('GET', f"/requests/{req['id']}/quotes")
                        assert current_quote['confirmed_by'] == 'buyer' and current_quote['version'] == 1
                        assert current_quote['values'] == quote['values'] and current_quote['evidence'] == quote['evidence']
                        replay_path = '/table-imports/' + provenance['provenance']['import_id'] + '/confirm'
                        replay_command = {'expected_revision': 2, 'acknowledge': True}
                        assert call('POST', replay_path, json=replay_command) == current_quote
                        assert call('GET', f"/requests/{req['id']}/quotes") == [current_quote]
                        import_replays.append((replay_path, replay_command, current_quote))
                    report['steps'].append(label+'_independent_worker_draft_readback_and_replay')
                phase='restart'
                restart()
                for result in verified:
                    assert call('GET',f"/operations/{result['operation_id']}")['remote_id']==result['remote_id']
                for replay_path, replay_command, current_quote in import_replays:
                    assert call('POST', replay_path, json=replay_command) == current_quote
                report['steps'].append('api_process_restart_retains_remote_ids')
                if backend == 'postgresql':
                    phase = 'business-database-audit'
                    report['business_database_audit'] = audit_business_database(database_url, verified)
                report.update(status='passed',operations=verified,post_attempts=len(wire['posts']),
                    real_erpnext_drafts_verified=len(verified),api_business_database='PostgreSQL' if backend == 'postgresql' else 'SQLite',real_erp_database='MariaDB',
                    fault_injection='loopback HTTP gateway returns 504 after ERP commits; API restarts, then fresh worker only reads')
        except Exception as error:
            report.update(status='failed', phase=phase, reason=safe_failure(error),
                network_attempted=True, http_evidence=wire['requests'])
        finally:stop(p);log.close()
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ephemeral-test',action='store_true')
    p.add_argument('--include-multi-item',action='store_true')
    p.add_argument('--business-database', choices=('sqlite', 'postgresql'), default='sqlite')
    p.add_argument('--credentials',type=Path,default=Path('.data/erp-sandbox.json'))
    p.add_argument('--env-file',type=Path,default=Path('.env.erp-sandbox'))
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    report={'status':'blocked','network_attempted':False,'reason':'EXPLICIT_EPHEMERAL_TEST_REQUIRED'};code=2
    if sys.flags.optimize:
        report['reason'] = 'OPTIMIZED_PYTHON_NOT_SUPPORTED'
    elif a.ephemeral_test:
        try:
            data=validate_lab(a.credentials,a.env_file)
            if a.include_multi_item and data.get('multi_skus') != ['PF-SANDBOX-ITEM','PF-SANDBOX-ITEM-2']:
                raise ValueError('LAB_CONFIGURATION_MISMATCH')
            if a.business_database == 'postgresql':
                postgres_test_url()
        except (OSError,ValueError,KeyError,TypeError):
            report['reason']='LAB_CONFIGURATION_MISMATCH'
        else:
            network_started = False
            try:
                save_source_files(a.output.parent, data)
                if a.include_multi_item:
                    save_multi_source(a.output.parent, data)
                network_started = True
                report=(exercise(data, a.business_database, True) if a.include_multi_item else
                        exercise(data) if a.business_database == 'sqlite' else exercise(data, 'postgresql'))
                code=0 if report.get('status')=='passed' else 1
            except Exception as error:
                report={'status':'failed','network_attempted':network_started,'reason':safe_failure(error)}
                code=1
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2));return code

if __name__=='__main__':raise SystemExit(main())
