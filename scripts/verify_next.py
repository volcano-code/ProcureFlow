"""Strict Next build + real browser E2E gate. Missing prerequisites return exit 2, never success."""
from __future__ import annotations
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'apps/web'

def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def run(output:Path)->int:
    output.mkdir(parents=True,exist_ok=True)
    report={'scope':'Next.js production build + native React browser E2E','status':'blocked','steps':[],
            'fallback_to_static_demo':False,'browser_verified':False,'live_erp':False,'live_model':False}
    processes=[];logs=[]
    def save(): (output/'next-gate.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def command(name,args,env,cwd=ROOT):
        result=subprocess.run(args,cwd=cwd,env=env,capture_output=True,text=True,timeout=240)
        (output/(name+'.log')).write_text(result.stdout+result.stderr)
        report['steps'].append({'name':name,'exit_code':result.returncode})
        if result.returncode: raise RuntimeError(name+' failed')
    try:
        if not shutil.which('node') or not shutil.which('npm') or not (WEB/'node_modules/next/dist/bin/next').is_file():
            report['reason']='NEXT_DEPENDENCIES_MISSING';save();return 2
        with tempfile.TemporaryDirectory(prefix='pf-next-e2e-') as tmp:
            api_port,web_port=port(),port()
            while web_port==api_port:web_port=port()
            api_url=f'http://127.0.0.1:{api_port}';web_url=f'http://127.0.0.1:{web_port}'
            env={**os.environ,'NEXT_PUBLIC_API_BASE_URL':api_url,'NEXT_TELEMETRY_DISABLED':'1',
                 'PF_DATA_DIR':tmp,'PF_DATABASE_URL':f'sqlite:///{tmp}/next.sqlite3','PF_MODE':'demo','PF_ERP_MODE':'mock',
                 'ERP_ALLOW_DRAFT_WRITES':'false','PF_REQUIRE_BROWSER':'1','PF_ALLOW_TEST_MUTATIONS':'1',
                 'PF_NEXT_TEST_URL':web_url,'PF_SCREENSHOT_DIR':str(output),
                 'PF_WEB_ORIGINS':web_url}
            env.pop('PF_AUTH_TOKENS',None)
            for key in ('ERP_API_KEY','ERP_API_SECRET','LLM_API_KEY'):env.pop(key,None)
            command('next-typecheck',['npm','run','typecheck'],env,cwd=WEB)
            command('next-build',['npm','run','build'],env,cwd=WEB)
            for name,args,cwd,url in [('api',[sys.executable,str(ROOT/'scripts/start.py'),'--port',str(api_port)],ROOT,api_url+'/health'),
                ('next',['node',str(WEB/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port',str(web_port)],WEB,web_url)]:
                log=(output/(name+'-server.log')).open('w');logs.append(log)
                proc=subprocess.Popen(args,env=env,cwd=cwd,stdout=log,stderr=log);processes.append(proc)
                for _ in range(200):
                    if proc.poll() is not None: raise RuntimeError(name+' server exited')
                    try:
                        if httpx.get(url,timeout=.3).status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError(name+' readiness timeout')
            command('next-browser',[sys.executable,'-m','pytest','apps/web/e2e/workbench_e2e.py','-q',
                     '--junitxml='+str(output/'next-browser.xml')],env)
            report.update(status='passed',browser_verified=True);return 0
    except (RuntimeError,OSError,subprocess.TimeoutExpired) as error:
        report.update(status='failed',reason=str(error));return 1
    finally:
        for proc in reversed(processes):
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        for log in logs:log.close()
        save();print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'evals/reports/next-gate')
    args=parser.parse_args();raise SystemExit(run(args.output.resolve()))
