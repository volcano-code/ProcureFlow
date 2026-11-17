"""Run the frontend's shared Node transport against disposable real HTTP (not browser E2E)."""
from pathlib import Path
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time
import httpx
ROOT=Path(__file__).resolve().parents[1]

def run(output: Path|None=None)->dict:
    if not shutil.which('node'): raise RuntimeError('Node.js 22+ is required')
    with tempfile.TemporaryDirectory(prefix='pf-web-http-') as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'PF_DATA_DIR':tmp,'PF_DATABASE_URL':f'sqlite:///{tmp}/app.sqlite3',
             'PF_MODE':'demo','PF_ERP_MODE':'mock','ERP_ALLOW_DRAFT_WRITES':'false',
             'PF_ALLOW_TEST_MUTATIONS':'1','PF_TEST_API_URL':f'http://127.0.0.1:{port}'}
        env.pop('PF_AUTH_TOKENS',None)
        env['ERP_ALLOW_DRAFT_WRITES'] = 'false'
        for key in ('ERP_API_KEY', 'ERP_API_SECRET', 'ERP_BASE_URL', 'ERP_COMPANY', 'LLM_API_KEY'):
            env.pop(key, None)
        with (Path(tmp)/'api.log').open('w') as log:
            process=subprocess.Popen([sys.executable,str(ROOT/'scripts/start.py'),'--port',str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                for _ in range(80):
                    try:
                        if httpx.get(env['PF_TEST_API_URL']+'/health',timeout=.2).status_code==200: break
                    except httpx.HTTPError: pass
                    if process.poll() is not None: raise RuntimeError('Disposable API exited')
                    time.sleep(.1)
                else: raise RuntimeError('Disposable API failed to start')
                result=subprocess.run(['node','--test','apps/web/tests/workflow.http.test.mjs'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=45)
                print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
                import sqlite3
                with sqlite3.connect(Path(tmp)/'mock-erp.sqlite3') as db:
                    drafts=db.execute('SELECT count(*) FROM drafts').fetchone()[0]
                report={'status':'passed' if result.returncode==0 else 'failed','node_exit_code':result.returncode,
                        'scope':'shared Next frontend fetch transport + real local HTTP; not browser/React E2E',
                        'mock_draft_count':drafts,'live_erp':False,'live_model':False,'stdout':result.stdout,'stderr':result.stderr}
                if output:
                    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                if result.returncode: raise RuntimeError('Frontend HTTP smoke failed')
                assert drafts==2
                return report
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill();process.wait()
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);args=parser.parse_args()
    run(args.output)
