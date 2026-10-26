"""Native UI reads a service-generated synthetic advice receipt; no route mocking.

The adapter fixture supplies text, not an evaluated LLM. HTTP/citations are real.
"""
import os,subprocess,sys,json,re
from pathlib import Path
from playwright.sync_api import expect
from workbench_e2e import page,prepare_advice,policy_api

SEED = r'''
import json,sys
from types import SimpleNamespace
from procureflow.advice import AdviceService
from procureflow.config import Settings
from procureflow.contracts import Principal,AdviceRunCommand
from procureflow.db import Database
from procureflow.erp import MockERP
from procureflow.service import ProcurementService
s=Settings();db=Database(s.database_url);service=ProcurementService(db,s,MockERP(s.data_dir/'mock-erp.sqlite3'))
buyer=Principal(user_id='buyer-01',tenant_id='demo',role='buyer')
request=service.get_request(buyer,sys.argv[1]);advice=AdviceService(service)
class Fixture:
    client=SimpleNamespace(close=lambda:None)
    def run(self,invoke,ids,observer=None):
        compare=invoke('get_comparison',{})
        doc=invoke('get_evidence',{'document_id':compare['quotes'][0]['document_id']})
        refs=invoke('search_references',{'query':'运费'})
        return dict(summary='合成适配器：运费未知需澄清，不自动批准。',
            evidence_ids=[doc['fragments'][0]['id'],refs['results'][0]['citation_id']],
            advisory_only=True,llm_used=False,semantic_factuality_verified=False,evidence_read_verified=True,
            model_calls=0,provider_attempts=0,tool_calls=3,trace=[],usage=None,usage_complete=False,cost=None,runtime='scripted-browser-fixture')
try:
    run=advice.reserve(buyer,request['id'],AdviceRunCommand(expected_version=request['version'],idempotency_key='browser-reference-advice-01'))
    result=advice.process(buyer,run['id'],Fixture)
    assert result['status']=='COMPLETED',result
    print(json.dumps({'id':result['id']}))
finally:db.engine.dispose()
'''


def test_native_advice_reference_citation_readback_and_withdrawal(page):
    assert os.getenv('PF_ALLOW_TEST_MUTATIONS')=='1'
    assert Path(os.environ['PF_DATA_DIR']).name.startswith('pf-next-e2e-')
    assert os.environ['PF_DATABASE_URL'].startswith('sqlite:')
    prepare_advice(page);base=policy_api(page)
    body={'expected_version':0,'title':'建议绑定的运费原文','source_label':'synthetic-browser',
        'content':'运费未知时应当澄清，不能默认为零。','reason':'建议引用原生界面测试'}
    headers={'Authorization':'Bearer demo-approver'}
    response=page.request.post(base+'/policy-library/documents/browser-advice/versions',headers=headers,data=body)
    assert response.status==201,response.text()
    with page.expect_response(lambda r:r.request.method=='GET' and bool(re.search(r'/requests/[^/]+/advice-runs$',r.url))) as result:
        page.get_by_test_id('refresh-advice').click()
    request_id=result.value.url.split('/requests/')[1].split('/')[0]
    process=subprocess.run([sys.executable,'-c',SEED,request_id],capture_output=True,text=True,timeout=45)
    assert process.returncode==0,process.stdout+process.stderr
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-reference-citation')).to_be_enabled()
    expect(page.get_by_test_id('advice-output')).to_contain_text('合成适配器')
    page.get_by_test_id('advice-reference-citation').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('运费未知时应当澄清')
    response=page.request.post(base+'/policy-library/documents/browser-advice/versions',headers=headers,
        data={**body,'expected_version':1,'withdrawn':True,'content':''})
    assert response.status==201
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-current')).to_contain_text('已失效')
    expect(page.get_by_test_id('advice-reference-citation')).to_be_disabled()
    expect(page.get_by_test_id('advice-output')).to_contain_text('合成适配器')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:page.screenshot(path=str(Path(output)/'reference-advice-mobile.png'),full_page=True)
