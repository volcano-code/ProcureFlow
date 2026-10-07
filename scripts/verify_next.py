"""Strict Next build + real browser E2E gate. Missing prerequisites return exit 2, never success."""
from __future__ import annotations
import argparse, json, os, shutil, socket, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
WEB=ROOT/'apps/web'

class BrowserUnavailable(RuntimeError):
    pass

def write_safe_pilot_junit(source:Path,target:Path):
    """Keep outcomes while withholding Playwright request/fill/error contents."""
    safe=ET.Element('testsuites')
    for suite in ET.parse(source).getroot().iter('testsuite'):
        output=ET.SubElement(safe,'testsuite',{key:suite.get(key,'0') for key in ('tests','failures','errors','skipped','time')})
        for case in suite.findall('testcase'):
            item=ET.SubElement(output,'testcase',{key:case.get(key,'') for key in ('classname','name','time')})
            for state in ('failure','error','skipped'):
                if case.find(state) is not None:ET.SubElement(item,state,{'message':'Pilot test did not pass; sensitive details withheld'})
    ET.ElementTree(safe).write(target,encoding='utf-8',xml_declaration=True)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def run(output:Path, *, pilot=False)->int:
    output.mkdir(parents=True,exist_ok=True)
    report={'scope':'Next.js production build + native React browser E2E','status':'blocked','steps':[],
            'fallback_to_static_demo':False,'browser_verified':False,'live_erp':False,'live_model':False,'auth_mode':'pilot' if pilot else 'demo'}
    processes=[];logs=[]
    def save(): (output/'next-gate.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def command(name,args,env,cwd=ROOT):
        result=subprocess.run(args,cwd=cwd,env=env,capture_output=True,text=True,timeout=240)
        # Pilot browser call logs can include a password-field fill value on
        # failure. Publish fixed status only, never raw Playwright output.
        (output/(name+'.log')).write_text((f'{name}: exit_code={result.returncode}\n' if pilot and name=='next-browser'
                                         else result.stdout+result.stderr))
        report['steps'].append({'name':name,'exit_code':result.returncode})
        if result.returncode:
            if name=='next-browser' and 'socket() failed: Operation not permitted' in result.stdout+result.stderr:
                raise BrowserUnavailable('CHROMIUM_UNIX_SOCKET_DENIED')
            raise RuntimeError(name+' failed')
    try:
        if not shutil.which('node') or not shutil.which('npm') or not (WEB/'node_modules/next/dist/bin/next').is_file():
            report['reason']='NEXT_DEPENDENCIES_MISSING';save();return 2
        with tempfile.TemporaryDirectory(prefix='pf-next-e2e-') as tmp:
            api_port,web_port=port(),port()
            while web_port==api_port:web_port=port()
            api_url=f'http://127.0.0.1:{api_port}';web_url=f'http://127.0.0.1:{web_port}'
            env={**{key:value for key,value in os.environ.items() if not key.startswith(('PF_','ERP_','LLM_'))},'NEXT_PUBLIC_API_BASE_URL':api_url,'NEXT_TELEMETRY_DISABLED':'1',
                 'PF_DATA_DIR':tmp,'PF_DATABASE_URL':f'sqlite:///{tmp}/next.sqlite3','PF_MODE':'pilot' if pilot else 'demo','PF_ERP_MODE':'mock',
                 'PYTHONPATH':str(ROOT/'services/api'),
                 'ERP_ALLOW_DRAFT_WRITES':'false','PF_REQUIRE_BROWSER':'1','PF_ALLOW_TEST_MUTATIONS':'1',
                 'PF_NEXT_TEST_URL':web_url,'PF_SCREENSHOT_DIR':str(output),
                 'PF_BROWSER_TRACE_DIR':str(output/'traces'),
                 'PF_WEB_ORIGINS':web_url}
            env.pop('PF_AUTH_TOKENS',None)
            for key in ('ERP_API_KEY','ERP_API_SECRET','LLM_API_KEY'):env.pop(key,None)
            if pilot:
                # Traces record request headers/bodies. Never publish pilot bearer
                # or one-use invitation plaintext, even from synthetic test users.
                env.pop('PF_BROWSER_TRACE_DIR',None)
                env.pop('PF_SCREENSHOT_DIR',None)
                command('pilot-migrate',[sys.executable,'-m','alembic','upgrade','head'],env,cwd=ROOT/'services/api')
            command('next-typecheck',['npm','run','typecheck'],env,cwd=WEB)
            command('next-build',['npm','run','build'],env,cwd=WEB)
            for name,args,cwd,url in [('api',[sys.executable,str(ROOT/'scripts/start.py'),'--port',str(api_port)],ROOT,api_url+'/health'),
                ('next',['node',str(WEB/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port',str(web_port)],WEB,web_url)]:
                log=(Path(tmp)/(name+'-server.log') if pilot else output/(name+'-server.log')).open('w');logs.append(log)
                proc=subprocess.Popen(args,env=env,cwd=cwd,stdout=log,stderr=log);processes.append(proc)
                for _ in range(200):
                    if proc.poll() is not None: raise RuntimeError(name+' server exited')
                    try:
                        if httpx.get(url,timeout=.3).status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError(name+' readiness timeout')
            junit=Path(tmp)/'pilot-browser.xml' if pilot else output/'next-browser.xml'
            try:
                command('next-browser',[sys.executable,'-m','pytest','apps/web/e2e/pilot_session_e2e.py' if pilot else 'apps/web/e2e/workbench_e2e.py','-q',
                         '--junitxml='+str(junit)],env)
            finally:
                if pilot and junit.is_file():
                    # Preserve test names/counts/outcomes, never assertion text,
                    # request details, page DOM, captured logs or tool call logs.
                    write_safe_pilot_junit(junit,output/'next-browser.xml')
            counts={key:0 for key in ('tests','failures','errors','skipped')}
            for suite in ET.parse(output/'next-browser.xml').getroot().iter('testsuite'):
                for key in counts:counts[key]+=int(suite.get(key,'0'))
            report['junit']=counts
            if counts['tests']<1 or any(counts[key] for key in ('failures','errors','skipped')):
                raise RuntimeError('NATIVE_BROWSER_TESTS_MISSING_OR_SKIPPED')
            report.update(status='passed',browser_verified=True);return 0
    except BrowserUnavailable as error:
        report.update(status='blocked',reason=str(error));return 2
    except (RuntimeError,OSError,subprocess.TimeoutExpired,ET.ParseError,ValueError) as error:
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
    parser.add_argument('--pilot',action='store_true',help='Run durable pilot login/lifecycle browser tests against a migrated disposable database')
    args=parser.parse_args();raise SystemExit(run(args.output.resolve(),pilot=args.pilot))
