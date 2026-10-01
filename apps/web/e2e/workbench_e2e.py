"""Strict native Next.js browser gates. No fallback to apps/demo, no skips."""
import os
import json
import re
import time
from pathlib import Path
import shutil
import pytest
from playwright.sync_api import sync_playwright, expect
URL=os.environ.get('PF_NEXT_TEST_URL')
if not URL or os.environ.get('PF_ALLOW_TEST_MUTATIONS')!='1':
    raise RuntimeError('Run scripts/verify_next.py with its disposable mock API')

@pytest.fixture
def page():
    with sync_playwright() as pw:
        executable=os.getenv('PF_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
        browser=pw.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        p=browser.new_page(viewport={'width':1440,'height':1100})
        errors=[];p.on('pageerror',lambda error:errors.append(str(error)))
        try:yield p
        finally:browser.close()
        assert errors==[],errors

def prepare(p):
    p.goto(URL)
    p.get_by_test_id('login-demo-buyer').click()
    expect(p.get_by_test_id('load-demo')).to_be_enabled()
    p.get_by_test_id('load-demo').click()
    expect(p.get_by_test_id('quote-row')).to_have_count(3)
    expect(p.get_by_test_id('quote-body')).to_contain_text('24200.00')
    expect(p.get_by_test_id('quote-body')).to_contain_text('未知')
    for i in range(3):
        row=p.get_by_test_id('quote-row').nth(i)
        row.get_by_test_id('confirm-quote').click()
        p.get_by_test_id('ack-confirm').check()
        p.get_by_test_id('submit-confirm').click()
        expect(p.get_by_test_id('submit-confirm')).to_have_count(0)
        expect(row.get_by_test_id('confirm-quote')).to_be_disabled()
    p.get_by_test_id('analyze').click()
    expect(p.get_by_test_id('proposal-body')).to_contain_text('24200.00')

def approve(p):
    p.get_by_label('切换演示身份').select_option('demo-approver')
    expect(p.get_by_test_id('identity')).to_have_text('approver')
    p.get_by_test_id('approve').click()
    expect(p.get_by_test_id('request-status')).to_have_text('已批准')
    p.get_by_label('切换演示身份').select_option('demo-buyer')
    expect(p.get_by_test_id('identity')).to_have_text('buyer')

def test_native_success_evidence_and_persisted_request(page):
    prepare(page)
    page.get_by_test_id('quote-row').filter(has_text='SUP-C').get_by_test_id('field-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('第 1 页')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('1180.00')
    approve(page);page.get_by_test_id('execute').click()
    expect(page.get_by_test_id('proposal-body')).to_contain_text('MOCK-SQ-')
    expect(page.get_by_test_id('request-status')).to_have_text('草稿已创建')
    # Credentials are intentionally kept in memory, not persisted to localStorage.
    page.reload();page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('proposal-body')).to_contain_text('MOCK-SQ-')
    page.get_by_test_id('verify-erp').click()
    expect(page.get_by_test_id('erp-verification')).to_contain_text('草稿与审批快照一致')
    expect(page.get_by_test_id('erp-verification')).to_contain_text('本次仅回读')
    expect(page.get_by_test_id('request-status')).to_have_text('草稿已创建')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(Path(output)/'next-workbench.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if output:page.screenshot(path=str(Path(output)/'next-workbench-mobile.png'),full_page=True)

def test_native_edit_request_invalidates_approval(page):
    prepare(page);approve(page)
    page.get_by_test_id('edit-request').click()
    page.get_by_test_id('request-form').locator('[name="quantity"]').fill('21')
    page.get_by_test_id('request-form').get_by_role('button',name='保存需求').click()
    expect(page.get_by_test_id('request-status')).to_have_text('审批已失效')
    expect(page.get_by_test_id('execute')).to_have_count(0)
    expect(page.get_by_test_id('request-meta')).to_contain_text('21 EA')

def test_native_rejection_blocks_execution(page):
    prepare(page)
    page.get_by_label('切换演示身份').select_option('demo-approver')
    page.get_by_test_id('reject').click()
    expect(page.get_by_test_id('request-status')).to_have_text('已拒绝')
    page.get_by_label('切换演示身份').select_option('demo-buyer')
    expect(page.get_by_test_id('execute')).to_have_count(0)

def test_native_identity_change_clears_evidence_and_prior_tenant(page):
    prepare(page)
    page.get_by_test_id('quote-row').filter(has_text='SUP-C').get_by_test_id('field-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('1180.00')
    page.get_by_label('访问令牌').fill('demo-other-tenant')
    page.get_by_role('button',name='登录',exact=True).click()
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    expect(page.get_by_test_id('evidence-body')).to_have_count(0)
    expect(page.get_by_test_id('proposal-body')).to_have_count(0)


class MockAdviceRoutes:
    """UI contract fixtures only: no live or backend LLM/provider execution is claimed."""
    def __init__(self, page, *, configured=True):
        self.configured=configured
        self.runs={}
        self.reservations=[]
        self.process_calls=[]
        self.pending_routes=[]
        self.hold_reservation=False
        self.hold_process=False
        self.outcome='COMPLETED'
        self.fail_history=False
        self.lose_process_response=False
        self.lose_process_before_claim=False
        self.cite_real_source=False
        self.quote_sources={}
        self.request_url=None
        self.history_reads=0
        page.route('**/api/v1/**',self.handle)

    def respond(self,route,value,status=200):
        route.fulfill(status=status,content_type='application/json',body='' if status==204 else json.dumps(value),
            headers={'Access-Control-Allow-Origin':URL,'Access-Control-Allow-Headers':'Authorization, Content-Type',
                     'Access-Control-Allow-Methods':'GET, POST, OPTIONS'})

    def handle(self,route):
        path=route.request.url.split('/api/v1',1)[1]
        if route.request.method=='OPTIONS' and (path=='/capabilities' or 'advice-runs' in path):
            self.respond(route,None,204)
            return
        if path=='/capabilities':
            original=route.fetch().json()
            self.respond(route,{**original,'advice_configured':self.configured,
                'advice_runtime':'langgraph-read-only-v1'})
            return
        quotes=re.fullmatch(r'/requests/([^/]+)/quotes',path)
        if quotes and route.request.method=='GET':
            values=route.fetch().json();self.quote_sources[quotes[1]]=values
            self.respond(route,values)
            return
        match=re.fullmatch(r'/requests/([^/]+)/advice-runs',path)
        if match:
            request_id=match[1]
            self.request_url=route.request.url.removesuffix('/advice-runs')
            if route.request.method=='GET':
                self.history_reads+=1
                if self.fail_history:
                    self.respond(route,{'error':{'code':'MOCK_HISTORY_UNAVAILABLE','message':'Mock history unavailable'}},503)
                else:self.respond(route,list(reversed(self.runs.get(request_id,[]))))
                return
            body=route.request.post_data_json
            self.reservations.append(body)
            assert re.fullmatch(r'[A-Za-z0-9_-]{8,80}',body['idempotency_key'])
            run={'id':f'mock-advice-{len(self.reservations)}','request_id':request_id,
                'request_version':body['expected_version'],'status':'PENDING','input_hash':'mock-input-hash',
                'created_at':'2026-10-01T00:00:00Z','started_at':None,'completed_at':None,
                'error_code':None,'output':None,'current':True,'stale_reason':None}
            self.runs.setdefault(request_id,[]).append(run)
            if self.hold_reservation:self.pending_routes.append((route,run))
            else:self.respond(route,run)
            return
        match=re.fullmatch(r'/advice-runs/([^/]+)/process',path)
        if match:
            self.process_calls.append(match[1])
            run=next(run for runs in self.runs.values() for run in runs if run['id']==match[1])
            if self.lose_process_before_claim:
                route.abort('failed')
                return
            run['status']='RUNNING';run['started_at']='2026-10-01T00:00:01Z'
            if self.hold_process:self.pending_routes.append((route,run))
            else:self.finish(route,run)
            return
        route.fallback()

    def finish(self,route,run):
        run['status']=self.outcome;run['completed_at']='2026-10-01T00:00:02Z'
        if self.outcome=='COMPLETED':
            evidence_id=(self.quote_sources[run['request_id']][0]['evidence']['unit_price']['fragment_id']
                         if self.cite_real_source else 'mock-document:fragment-1')
            run['output']={'summary':'MOCK PROVIDER：请人工核对运费，建议不能代替采购审批。',
                'evidence_ids':[evidence_id],'runtime':'langgraph-read-only-v1',
                'llm_used':True,'advisory_only':True,'semantic_factuality_verified':False,
                'evidence_read_verified':True,
                'model_calls':2,'tool_calls':1,'trace':[{'type':'tool_completed','tool':'get_comparison'}],
                'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30},
                'usage_complete':True,'cost':None}
        else:run['error_code']='MOCK_'+self.outcome
        if self.lose_process_response:route.abort('failed')
        else:self.respond(route,run)


def wait_for_mock(page, condition):
    deadline=time.monotonic()+5
    while not condition() and time.monotonic()<deadline:page.wait_for_timeout(10)
    assert condition(), 'Mock route did not arrive'


def prepare_advice(page):
    page.goto(URL)
    page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('load-demo')).to_be_enabled()
    page.get_by_test_id('load-demo').click()
    expect(page.get_by_test_id('quote-row')).to_have_count(3)
    expect(page.get_by_test_id('advice-empty')).to_be_visible()


def advice_screenshot(page,name):
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        page.get_by_test_id('advice-panel').screenshot(path=str(Path(output)/name))


def test_native_advice_real_backend_unconfigured_history(page):
    """Real HTTP capability/list endpoints; the disposable server has no model key."""
    prepare_advice(page)
    expect(page.get_by_test_id('advice-provider')).to_contain_text('模型提供方未配置')
    expect(page.get_by_test_id('new-advice')).to_be_disabled()
    with page.expect_response(lambda response: response.request.method=='GET' and
                              re.search(r'/api/v1/requests/[^/]+/advice-runs$',response.url)) as response:
        page.get_by_test_id('refresh-advice').click()
    assert response.value.status==200 and response.value.json()==[]
    expect(page.get_by_test_id('advice-empty')).to_be_visible()


def test_native_advice_unconfigured_never_calls_model(page):
    mock=MockAdviceRoutes(page,configured=False)
    prepare_advice(page)
    expect(page.get_by_test_id('advice-provider')).to_contain_text('模型提供方未配置')
    expect(page.get_by_test_id('new-advice')).to_be_disabled()
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-empty')).to_be_visible()
    assert mock.reservations==[] and mock.process_calls==[]


def test_native_advice_mock_provider_persisted_history_and_no_replay(page):
    mock=MockAdviceRoutes(page);mock.hold_process=True
    prepare_advice(page)
    version=int(re.search(r'v(\d+)',page.get_by_test_id('request-meta').inner_text())[1])
    expect(page.get_by_test_id('advice-provider')).to_contain_text('尚未验证连通性或质量')
    expect(page.get_by_test_id('advice-panel')).to_contain_text('运行方式：langgraph-read-only-v1')
    # Wait for actionability and dispatch the same-tick clicks atomically. An SSE history
    # refresh may disable the button between separate readiness and evaluate calls.
    page.wait_for_function("""() => {
        const button = document.querySelector('[data-testid="new-advice"]');
        if (!button || button.disabled) return false;
        button.click();
        button.click();
        return true;
    }""")
    expect(page.get_by_test_id('new-advice')).to_be_disabled()
    expect(page.get_by_test_id('advice-run')).to_have_count(1)
    assert len(mock.reservations)==1 and mock.reservations[0]['expected_version']==version
    # Let Playwright dispatch the pending process route before completing the mock provider.
    wait_for_mock(page,lambda: len(mock.process_calls)==1)
    expect(page.get_by_test_id('advice-status')).to_have_text('处理请求已发送，模型调用可能进行中')
    expect(page.get_by_test_id('advice-panel')).not_to_contain_text('仅已预留，尚未执行')
    route,run=mock.pending_routes.pop();mock.finish(route,run)
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    expect(page.get_by_test_id('advice-output')).to_contain_text('语义真实性未验证')
    expect(page.get_by_test_id('advice-citations')).to_contain_text('mock-document:fragment-1')
    expect(page.get_by_test_id('advice-output')).to_contain_text('费用未知')
    advice_screenshot(page,'next-advice-mock-completed.png')
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-status')).to_have_text('已完成')
    page.reload();page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    page.get_by_label('切换演示身份').select_option('demo-approver')
    expect(page.get_by_test_id('advice-read-only')).to_be_visible()
    expect(page.get_by_test_id('new-advice')).to_have_count(0)
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    page.get_by_label('切换演示身份').select_option('demo-buyer')
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    assert len(mock.reservations)==1 and len(mock.process_calls)==1
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:page.screenshot(path=str(Path(output)/'next-advice-mock-mobile.png'),full_page=True)


def test_native_advice_mock_failure_interrupted_stale_and_recovery(page):
    mock=MockAdviceRoutes(page);mock.outcome='FAILED'
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-failure')).to_contain_text('MOCK_FAILED')
    expect(page.get_by_test_id('new-advice')).to_be_enabled()
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-status')).to_have_text('生成失败')
    assert len(mock.process_calls)==1
    mock.outcome='INTERRUPTED'
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-run')).to_have_count(2)
    expect(page.get_by_test_id('advice-status').first).to_have_text('执行中断，禁止重放')
    expect(page.get_by_test_id('process-advice')).to_have_count(0)
    page.reload();page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('advice-run')).to_have_count(2)
    assert len(mock.process_calls)==2
    mock.outcome='COMPLETED'
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    for runs in mock.runs.values():
        for run in runs:run['current']=False;run['stale_reason']='SOURCE_CHANGED'
    page.get_by_test_id('edit-request').click()
    page.get_by_test_id('request-form').locator('[name="quantity"]').fill('21')
    page.get_by_test_id('request-form').get_by_role('button',name='保存需求').click()
    expect(page.get_by_test_id('advice-stale').first).to_contain_text('SOURCE_CHANGED')
    expect(page.get_by_test_id('advice-current').first).to_have_text('历史结果，已失效')
    assert len(mock.process_calls)==3
    advice_screenshot(page,'next-advice-mock-stale.png')


def test_native_advice_mock_late_reservation_cannot_process_after_request_switch(page):
    mock=MockAdviceRoutes(page);mock.hold_reservation=True
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    page.get_by_role('button',name='＋ 新建采购需求',exact=True).click()
    page.get_by_test_id('request-form').locator('[name="title"]').fill('隔离建议的第二项需求')
    page.get_by_test_id('request-form').get_by_role('button',name='保存需求').click()
    expect(page.get_by_test_id('request-meta')).to_contain_text('v1')
    expect(page.get_by_test_id('advice-empty')).to_be_visible()
    wait_for_mock(page,lambda: len(mock.pending_routes)==1)
    route,run=mock.pending_routes.pop();mock.respond(route,run)
    page.wait_for_timeout(100)
    assert mock.process_calls==[]
    expect(page.get_by_test_id('advice-run')).to_have_count(0)
    # Returning shows the saved reservation; only an explicit click may process it.
    page.locator('#request-list button').filter(has_text='研发工位支架采购').first.click()
    expect(page.get_by_test_id('advice-status')).to_have_text('已预留，尚未调用模型')
    assert mock.process_calls==[]
    page.get_by_test_id('process-advice').click()
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    assert len(mock.process_calls)==1


def test_native_advice_mock_late_process_cannot_leak_across_identity(page):
    mock=MockAdviceRoutes(page);mock.hold_process=True
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-run')).to_have_count(1)
    page.get_by_label('访问令牌').fill('demo-other-tenant')
    page.get_by_role('button',name='登录',exact=True).click()
    expect(page.get_by_test_id('advice-panel')).to_have_count(0)
    wait_for_mock(page,lambda: len(mock.pending_routes)==1)
    route,run=mock.pending_routes.pop();mock.finish(route,run)
    page.wait_for_timeout(100)
    expect(page.get_by_test_id('advice-output')).to_have_count(0)
    expect(page.get_by_test_id('advice-panel')).to_have_count(0)
    assert len(mock.process_calls)==1


def test_native_advice_mock_read_failure_is_recoverable_without_writes(page):
    mock=MockAdviceRoutes(page)
    prepare_advice(page)
    mock.fail_history=True
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-read-error')).to_contain_text('MOCK_HISTORY_UNAVAILABLE')
    expect(page.get_by_test_id('new-advice')).to_be_disabled()
    mock.fail_history=False
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-empty')).to_be_visible()
    expect(page.get_by_test_id('new-advice')).to_be_enabled()
    assert mock.reservations==[] and mock.process_calls==[]


def test_native_advice_mock_lost_process_response_reads_receipt_without_replay(page):
    mock=MockAdviceRoutes(page);mock.lose_process_response=True
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-error')).to_contain_text('未自动重试')
    expect(page.get_by_test_id('advice-output')).to_contain_text('MOCK PROVIDER')
    expect(page.get_by_test_id('advice-status')).to_have_text('已完成')
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('advice-status')).to_have_text('已完成')
    assert len(mock.reservations)==1 and len(mock.process_calls)==1
    # A racing PENDING read after an uncertain POST cannot prove no model call.
    mock.lose_process_before_claim=True
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-error')).to_contain_text('未自动重试')
    expect(page.get_by_test_id('process-advice')).to_be_enabled()
    expect(page.get_by_test_id('advice-status').first).to_have_text('处理请求已发送，模型调用可能进行中')
    page.get_by_test_id('refresh-advice').click()
    expect(page.get_by_test_id('process-advice')).to_be_enabled()
    expect(page.get_by_test_id('advice-status').first).to_have_text('处理请求已发送，模型调用可能进行中')
    assert len(mock.reservations)==2 and len(mock.process_calls)==2
    mock.lose_process_before_claim=False;mock.lose_process_response=False
    page.get_by_test_id('process-advice').click()
    expect(page.get_by_test_id('advice-status').first).to_have_text('已完成')
    assert len(mock.reservations)==2 and mock.process_calls[-1]==mock.process_calls[-2]


def test_native_advice_mock_citation_opens_verified_real_source(page):
    mock=MockAdviceRoutes(page);mock.cite_real_source=True
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-citation')).to_have_count(1)
    source=next(iter(mock.runs.values()))[-1]['output']['evidence_ids'][0]
    with page.expect_response(lambda response: response.request.method=='GET' and response.url.endswith('/evidence')) as response:
        page.get_by_test_id('advice-citation').click()
    assert response.value.status==200
    fragment=next(item for item in response.value.json()['fragments'] if item['id']==source)
    expect(page.get_by_test_id('evidence-body')).to_contain_text(fragment['text'])
    expect(page.get_by_test_id('evidence-body')).to_contain_text(response.value.json()['sha256'])
    # A later source-read error must not fabricate a replacement fragment.
    page.route('**/api/v1/documents/*/evidence',lambda route: mock.respond(route,
        {'error':{'code':'SOURCE_INTEGRITY_FAILED','message':'Mock missing source'}},409))
    page.get_by_test_id('advice-citation').click()
    expect(page.get_by_test_id('advice-citation-error')).to_contain_text('SOURCE_INTEGRITY_FAILED')


def test_native_advice_mock_business_audit_event_refreshes_freshness(page):
    mock=MockAdviceRoutes(page)
    prepare_advice(page)
    page.get_by_test_id('new-advice').click()
    expect(page.get_by_test_id('advice-status')).to_have_text('已完成')
    expect(page.get_by_test_id('stream-status')).to_contain_text('live')
    before=mock.history_reads
    headers={'Authorization':'Bearer demo-buyer'}
    request=page.request.get(mock.request_url,headers=headers).json()
    for runs in mock.runs.values():
        for run in runs:run['current']=False;run['stale_reason']='ADVICE_INPUT_CHANGED'
    body={key:request[key] for key in ('title','sku','quantity','uom','budget','max_delivery_days','currency')}
    body.update(quantity='21',expected_version=request['version'])
    changed=page.request.put(mock.request_url,headers=headers,data=body)
    assert changed.status==200
    expect(page.get_by_test_id('advice-current')).to_have_text('历史结果，已失效')
    assert mock.history_reads>before and len(mock.process_calls)==1
