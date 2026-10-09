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
def page(request):
    with sync_playwright() as pw:
        executable=os.getenv('PF_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
        browser=pw.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        p=browser.new_page(viewport={'width':1440,'height':1100})
        errors=[];p.on('pageerror',lambda error:errors.append(str(error)))
        trace_dir=os.getenv('PF_BROWSER_TRACE_DIR')
        traced=bool(trace_dir and request.node.name.startswith('test_native_policy_'))
        policy_reads=[]
        if traced:
            p.context.tracing.start(screenshots=True,snapshots=True,sources=True)
            # Disposable synthetic policy cases only. Capture the event order and disabled
            # state even when a browser click returns without invoking React's handler.
            p.add_init_script('''(() => {
                const events = window.__policyEvents = [];
                const record = (type, target) => {
                    const panel = document.querySelector('[data-testid="policy-panel"]');
                    if (!panel) return;
                    const trigger = panel.querySelector('[data-testid="new-policy"]');
                    const dialog = panel.querySelector('dialog');
                    events.push({at: Date.now(), type,
                        target: target instanceof Element ? target.getAttribute('data-testid') ||
                            target.getAttribute('aria-label') || target.tagName : 'window',
                        busy: panel.getAttribute('aria-busy'), triggerDisabled: trigger?.disabled,
                        dialogOpen: dialog?.open ?? false,
                        formPresent: !!panel.querySelector('[data-testid="policy-form"]'),
                        version: dialog?.querySelector('h2')?.textContent,
                        error: panel.querySelector('[data-testid="policy-error"]')?.textContent});
                    if (events.length > 500) events.shift();
                };
                for (const type of ['pointerdown', 'pointerup', 'click', 'focusin', 'cancel', 'close']) {
                    document.addEventListener(type, event => record(type, event.target), true);
                }
                for (const type of ['focus', 'blur']) window.addEventListener(type, event => record(type, event.target));
                new MutationObserver(records => {
                    if (records.some(item => item.target instanceof Element &&
                        item.target.closest('[data-testid="policy-panel"]'))) record('policy-render', null);
                }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
            })();''')
            p.on('request',lambda value: policy_reads.append({'at':time.time()*1000,'event':'request',
                'method':value.method,'url':value.url}) if '/api/v1/policy' in value.url else None)
            p.on('response',lambda value: policy_reads.append({'at':time.time()*1000,'event':'response',
                'status':value.status,'url':value.url}) if '/api/v1/policy' in value.url else None)
        try:yield p
        finally:
            try:
                if traced:
                    target=Path(trace_dir);target.mkdir(parents=True,exist_ok=True)
                    stem=target/request.node.name
                    try:
                        events=p.evaluate('window.__policyEvents || []')
                        stem.with_suffix('.json').write_text(json.dumps({'events':events,'reads':policy_reads,
                            'page_errors':errors},ensure_ascii=False,indent=2)+'\n')
                        stem.with_suffix('.html').write_text(p.content())
                        p.screenshot(path=str(stem.with_suffix('.png')),full_page=True,timeout=5000)
                    except Exception as error:
                        print(f'Policy diagnostic capture failed: {error}')
                    p.context.tracing.stop(path=str(stem.with_suffix('.zip')))
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
        self.policy=None
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
        if path=='/policy' and route.request.method=='GET':
            self.policy=route.fetch().json()
            self.respond(route,self.policy)
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
                'policy_version':self.policy['version'],'policy_hash':self.policy['policy_hash'],
                'quote_collection_hash':'mock-quote-collection-hash','input_snapshot':{'policy':self.policy},
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
    version=int(re.search(r'v(\d+)',page.get_by_test_id('request-meta').inner_text())[1])
    for runs in mock.runs.values():
        for run in runs:run['current']=False;run['stale_reason']='SOURCE_CHANGED'
    page.get_by_test_id('edit-request').click()
    page.get_by_test_id('request-form').locator('[name="quantity"]').fill('21')
    page.get_by_test_id('request-form').get_by_role('button',name='保存需求').click()
    # A history refresh can expose the mock's stale flags on the old panel before
    # save refreshes the request. Wait for the version-keyed remount and its history.
    expect(page.get_by_test_id('request-form')).to_have_count(0)
    expect(page.get_by_test_id('request-meta')).to_contain_text(re.compile(rf'\bv{version+1}$'))
    expect(page.get_by_test_id('request-meta')).to_contain_text('21 EA')
    expect(page.get_by_test_id('advice-panel')).to_contain_text(f'绑定需求 v{version+1}、')
    expect(page.get_by_test_id('advice-panel')).to_have_attribute('aria-busy','false')
    expect(page.get_by_test_id('advice-run')).to_have_count(3)
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


def policy_api(page):
    # Resolve from the actual app request, never from an assumed port.
    with page.expect_response(lambda response: response.request.method == 'GET' and response.url.endswith('/api/v1/policy')) as response:
        page.get_by_test_id('refresh-policy').click()
    return response.value.url.removesuffix('/policy')


def publish_policy_api(page, base, **changes):
    headers={'Authorization':'Bearer demo-approver'}
    current=page.request.get(base+'/policy',headers=headers).json()
    body={'expected_version':current['latest_version'],'budget_cap':None,'max_delivery_days':None,
          'minimum_valid_quotes':1,'effective_at':None,'reason':'Disposable browser gate policy reset',**changes}
    response=page.request.post(base+'/policy/versions',headers=headers,data=body)
    assert response.status == 201, response.text()
    return response.json()


def policy_screenshot(page, name):
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:
        Path(output).mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(Path(output)/name),full_page=True)


def test_native_policy_role_history_stale_evaluation_and_strictest_limits(page):
    prepare(page)
    approve(page)
    base=policy_api(page)
    before=page.request.get(base+'/policy',headers={'Authorization':'Bearer demo-buyer'}).json()
    try:
        expect(page.get_by_test_id('policy-read-only')).to_be_visible()
        expect(page.get_by_test_id('new-policy')).to_have_count(0)
        expect(page.get_by_test_id('evaluation-current').first).to_have_text('绑定仍有效')
        page.get_by_label('切换演示身份').select_option('demo-approver')
        expect(page.get_by_test_id('new-policy')).to_be_enabled()
        page.get_by_test_id('new-policy').click()
        form=page.get_by_test_id('policy-form')
        form.locator('[name="budget_cap"]').fill('23000.00')
        form.locator('[name="max_delivery_days"]').fill('7')
        form.locator('[name="minimum_valid_quotes"]').fill('3')
        form.locator('[name="reason"]').fill('Browser gate: stricter budget, delivery and supplier competition')
        page.get_by_test_id('publish-policy').click()
        expect(form).to_have_count(0)
        expect(page.get_by_test_id('effective-policy')).to_contain_text(f'当前生效 v{before["latest_version"]+1}')
        expect(page.get_by_test_id('effective-limits')).to_contain_text('实际预算 ≤ ¥23000.00')
        expect(page.get_by_test_id('effective-limits')).to_contain_text('实际交期 ≤ 7 天')
        expect(page.get_by_test_id('proposal-stale')).to_be_visible()
        expect(page.get_by_test_id('approve')).to_be_disabled()
        expect(page.get_by_test_id('evaluation-current').first).to_have_text('已失效，仅供历史查阅')
        page.get_by_test_id('approval-history').locator('summary').first.click()
        expect(page.get_by_test_id('approval-current').first).to_have_text('已失效，仅供审计')
        expect(page.get_by_test_id('approval-receipt').first).to_contain_text('原快照哈希')
        page.get_by_test_id('policy-history').locator('summary').first.click()
        expect(page.get_by_test_id('policy-version').first).to_contain_text('Browser gate: stricter budget')
        assert page.get_by_test_id('policy-history').locator('input, textarea, button').count() == 0
        policy_screenshot(page,'next-policy-history.png')
        page.get_by_label('切换演示身份').select_option('demo-buyer')
        expect(page.get_by_test_id('execute')).to_have_count(0)
        page.get_by_test_id('analyze').click()
        expect(page.get_by_test_id('evaluation-violations').first).to_contain_text('INSUFFICIENT_VALID_QUOTES')
        expect(page.get_by_test_id('request-status')).to_have_text('规则未通过')
        page.get_by_test_id('evaluation').first.locator('summary').first.click()
        expect(page.get_by_test_id('evaluation').first).to_contain_text('BUDGET_EXCEEDED')
        expect(page.get_by_test_id('evaluation').first).to_contain_text('DELIVERY_EXCEEDS_LIMIT')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        publish_policy_api(page,base)


def test_native_policy_conflict_requires_refresh_and_deliberate_reentry(page):
    prepare(page)
    base=policy_api(page)
    try:
        page.get_by_label('切换演示身份').select_option('demo-approver')
        page.get_by_test_id('new-policy').click()
        page.get_by_test_id('policy-form').locator('[name="reason"]').fill('Browser gate stale editor must not overwrite a policy')
        winner=publish_policy_api(page,base,reason='Concurrent approver publication wins')
        # Keep the originally displayed form open; expected_version must not silently advance.
        page.get_by_test_id('publish-policy').click()
        expect(page.get_by_test_id('policy-error')).to_contain_text('已被其他审批人更新')
        expect(page.get_by_test_id('publish-policy')).to_be_disabled()
        expect(page.get_by_test_id('reload-policy-form')).to_be_enabled()
        expect(page.get_by_test_id('policy-dialog')).to_be_visible()
        policy_screenshot(page,'next-policy-conflict.png')
        latest=page.request.get(base+'/policy',headers={'Authorization':'Bearer demo-approver'}).json()
        assert latest['latest_version']==winner['version']
        page.get_by_test_id('reload-policy-form').click()
        expect(page.get_by_test_id('policy-form').locator('[name="reason"]')).to_have_value('')
        page.get_by_label('关闭策略表单').click()
        expect(page.get_by_test_id('policy-form')).to_have_count(0)
        page.get_by_test_id('new-policy').click()
        expect(page.get_by_test_id('policy-form').locator('[name="reason"]')).to_have_value('')
        page.get_by_role('button',name='取消',exact=True).click()
        expect(page.get_by_test_id('policy-form')).to_have_count(0)

        # Force the focus/read lifecycle overlap rather than retrying a missed click:
        # a focus-triggered GET starts after pointer-down and finishes after click.
        # Cached policy permits opening; reads still block publication, and never
        # replace the captured version or the user's draft. Exercise each dismissal.
        pending=[]
        hold=False
        def policy_read(route):
            if hold:pending.append(route)
            else:route.continue_()
        page.route(base+'/policy',policy_read)
        try:
            for dismissal in ('close','cancel','escape'):
                panel=page.get_by_test_id('policy-panel')
                trigger=page.get_by_test_id('new-policy')
                expect(panel).to_have_attribute('aria-busy','false')
                expect(trigger).to_be_enabled()
                trigger.hover()
                hold=True
                page.mouse.down()
                with page.expect_request(lambda value:value.method=='GET' and value.url==base+'/policy'):
                    page.evaluate('window.dispatchEvent(new Event("focus"))')
                expect(panel).to_have_attribute('aria-busy','true')
                page.mouse.up()
                form=page.get_by_test_id('policy-form')
                expect(page.get_by_test_id('policy-dialog')).to_be_visible()
                expect(form.locator('[name="reason"]')).to_have_value('')
                expect(page.get_by_test_id('publish-policy')).to_be_disabled()
                title=page.get_by_role('heading',name=re.compile('发布策略 v'))
                expected_title=title.inner_text()
                reason=f'Draft retained through focus refresh and {dismissal}'
                form.locator('[name="reason"]').fill(reason)
                assert pending, 'The focus read must be held while reopening the editor'
                hold=False
                for route in pending:route.continue_()
                pending.clear()
                expect(panel).to_have_attribute('aria-busy','false')
                expect(page.get_by_test_id('publish-policy')).to_be_enabled()
                expect(title).to_have_text(expected_title)
                expect(form.locator('[name="reason"]')).to_have_value(reason)
                if dismissal=='close':page.get_by_label('关闭策略表单').click()
                elif dismissal=='cancel':page.get_by_role('button',name='取消',exact=True).click()
                else:page.keyboard.press('Escape')
                expect(form).to_have_count(0)
        finally:
            hold=False
            page.mouse.up()
            page.unroute(base+'/policy',policy_read)
            for route in pending:route.continue_()

        # A read failure remains fail-closed after closing the dialog and during
        # a recovery read. Clearing a form error must not clear this safety state.
        page.get_by_test_id('new-policy').click()
        page.get_by_test_id('policy-form').locator('[name="reason"]').fill('Do not publish with an unreadable policy')
        def fail_policy_read(route):
            route.fulfill(status=503,content_type='application/json',
                body=json.dumps({'error':{'code':'MOCK_POLICY_UNAVAILABLE','message':'Policy read unavailable'}}),
                headers={'Access-Control-Allow-Origin':URL})
        page.route(base+'/policy',fail_policy_read)
        page.evaluate('window.dispatchEvent(new Event("focus"))')
        expect(page.get_by_test_id('policy-error')).to_contain_text('策略读取失败')
        expect(page.get_by_test_id('publish-policy')).to_be_disabled()
        page.get_by_label('关闭策略表单').click()
        expect(page.get_by_test_id('policy-error')).to_contain_text('策略读取失败')
        expect(page.get_by_test_id('new-policy')).to_be_disabled()
        page.unroute(base+'/policy',fail_policy_read)
        hold=True
        page.route(base+'/policy',policy_read)
        try:
            with page.expect_request(lambda value:value.method=='GET' and value.url==base+'/policy'):
                page.get_by_test_id('refresh-policy').click()
            expect(page.get_by_test_id('policy-panel')).to_have_attribute('aria-busy','true')
            expect(page.get_by_test_id('new-policy')).to_be_disabled()
            hold=False
            assert pending, 'The recovery read must be held before checking the disabled trigger'
            for route in pending:route.continue_()
            pending.clear()
            expect(page.get_by_test_id('new-policy')).to_be_enabled()
            page.get_by_test_id('new-policy').click()
            expect(page.get_by_test_id('policy-form').locator('[name="reason"]')).to_have_value('')
            page.get_by_role('button',name='取消',exact=True).click()
            expect(page.get_by_test_id('policy-form')).to_have_count(0)
            latest=page.request.get(base+'/policy',headers={'Authorization':'Bearer demo-approver'}).json()
            assert latest['latest_version']==winner['version'], 'Read, recovery and re-entry must never publish'
        finally:
            hold=False
            page.unroute(base+'/policy',policy_read)
            for route in pending:route.continue_()
    finally:
        publish_policy_api(page,base)


def test_native_policy_future_version_does_not_activate_early(page):
    prepare(page)
    base=policy_api(page)
    before=page.request.get(base+'/policy',headers={'Authorization':'Bearer demo-buyer'}).json()
    try:
        page.get_by_label('切换演示身份').select_option('demo-approver')
        page.get_by_test_id('new-policy').click()
        form=page.get_by_test_id('policy-form')
        form.locator('[name="timing"]').select_option('scheduled')
        # Browser fixture uses Chromium's configured local zone; derive the local form value there.
        date=page.evaluate('new Date(Date.now()+3600000).toLocaleString("sv-SE").slice(0,16).replace(" ","T")')
        form.locator('[name="effective_at"]').fill(date)
        form.locator('[name="budget_cap"]').fill('1.00')
        form.locator('[name="reason"]').fill('Browser gate future scheduled policy must not activate early')
        expect(page.get_by_test_id('policy-dialog')).to_be_visible()
        policy_screenshot(page,'next-policy-scheduled.png')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        policy_screenshot(page,'next-policy-mobile.png')
        page.set_viewport_size({'width':1440,'height':1100})
        page.get_by_test_id('publish-policy').click()
        expect(form).to_have_count(0)
        expect(page.get_by_test_id('policy-notice')).to_contain_text('将在')
        expect(page.get_by_test_id('effective-policy')).to_contain_text(f'当前生效 v{before["version"]}')
        page.get_by_test_id('policy-history').locator('summary').first.click()
        expect(page.get_by_test_id('policy-version').first).to_contain_text('已发布，待生效')
        expect(page.get_by_test_id('approve')).to_be_enabled()
        expect(page.get_by_test_id('evaluation-current').first).to_have_text('绑定仍有效')
    finally:
        # A later effective revision supersedes the lower scheduled version monotonically.
        publish_policy_api(page,base)


# Ordinary table import is a separate, explicit mapping step; never a quote confirmation.
def prepare_table_import(page):
    page.goto(URL)
    page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('load-demo')).to_be_enabled()
    page.get_by_role('button', name='＋ 新建采购需求').click()
    form=page.get_by_test_id('request-form')
    form.locator('[name="title"]').fill('普通表格浏览器验收')
    form.get_by_role('button', name='保存需求').click()
    expect(page.get_by_test_id('request-form')).to_have_count(0)
    expect(page.get_by_test_id('open-table-import')).to_be_enabled()
    expect(page.get_by_test_id('quote-row')).to_have_count(0)


def upload_table(page, name, contents):
    page.get_by_test_id('open-table-import').click()
    page.get_by_test_id('table-import-file').set_input_files({'name':name,
        'mimeType':'text/csv' if name.endswith('.csv') else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'buffer':contents})
    page.get_by_test_id('upload-table-preview').click()
    expect(page.get_by_test_id('table-sheet')).to_be_visible()


def table_csv():
    return ('Supplier ID,SKU,Quantity,Unit,Unit Price,Tax Mode,Tax Rate,Freight,Discount,Delivery Days,Currency\n'
            'SUP-TABLE,STAND-01,20,EA,1100.25,included,0.13,,0,7,CNY\n'
            'SUP-FORMULA,STAND-01,20,EA,=1000+10,,,0,0,9,CNY\n').encode()


def test_native_table_csv_maps_source_unknown_freight_and_never_auto_confirms(page):
    prepare_table_import(page)
    upload_table(page,'ordinary-supplier.csv',table_csv())
    expect(page.get_by_test_id('table-map-supplier_id')).to_have_value('A')
    expect(page.get_by_test_id('table-map-unit_price')).to_have_value('E')
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    expect(page.get_by_test_id('confirm-table-import')).to_have_count(0)
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-result-supplier_id')).to_contain_text('SUP-TABLE')
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('1100.25')
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('E2')
    expect(page.get_by_test_id('table-result-shipping_cost')).to_contain_text('未知')
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.get_by_test_id('table-import-ack').check()
    page.get_by_test_id('table-map-discount').select_option('')
    expect(page.get_by_test_id('table-preview-dirty')).to_be_visible()
    expect(page.get_by_test_id('table-import-ack')).not_to_be_checked()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.get_by_test_id('table-map-discount').select_option('I')
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-preview-dirty')).to_have_count(0)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:page.screenshot(path=str(Path(output)/'next-table-import-mobile.png'),full_page=True)
    page.set_viewport_size({'width':1440,'height':1100})
    page.get_by_test_id('table-import-ack').check()
    calls=[]
    page.on('request',lambda request:calls.append(request.url) if request.method=='POST' and '/table-imports/' in request.url and request.url.endswith('/confirm') else None)
    page.get_by_test_id('confirm-table-import').evaluate('(button)=>{button.click();button.click();}')
    expect(page.get_by_test_id('table-import-success')).to_contain_text('本次导入操作只创建待核对报价')
    page.get_by_test_id('finish-table-import').click()
    expect(page.get_by_test_id('quote-row')).to_have_count(1)
    expect(page.get_by_test_id('quote-row')).to_contain_text('待核对')
    expect(page.get_by_test_id('confirm-quote')).to_be_enabled()
    assert len(calls)==1
    page.get_by_test_id('field-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('E2')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('1100.25')
    page.get_by_test_id('open-table-import').click()
    page.get_by_test_id('table-import-file').set_input_files({'name':'ordinary-supplier.csv','mimeType':'text/csv','buffer':table_csv()})
    page.get_by_test_id('upload-table-preview').click()
    expect(page.get_by_test_id('table-import-success')).to_be_visible()
    page.get_by_test_id('finish-table-import').click()
    expect(page.get_by_test_id('quote-row')).to_have_count(1)
    assert len(calls)==1


def table_xlsx():
    """Minimal OOXML fixture using only stdlib, matching the server's dependency contract."""
    from io import BytesIO
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    sheets=[('说明',[['合成验收，非生产报价']]),('供应商报价',[
        ['供应商报价单'],
        ['供应商编码','型号','数量','单位','单价','税价模式','税率','运费','折扣金额','交期天数','币种'],
        ['SUP-XLSX','STAND-01','20','EA','1080.50','含税','13%','600','0','7','CNY'],
        ['SUP-XLSX-FORMULA','STAND-01','20','EA','=1080+5','','','','0','7','CNY']])]
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'+''.join(
            f'<sheet name="{name}" sheetId="{index}" r:id="r{index}"/>' for index,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(
            f'<Relationship Id="r{index}" Target="worksheets/sheet{index}.xml" Type="{rel}/worksheet"/>' for index in (1,2))+'</Relationships>')
        for index,(_,rows) in enumerate(sheets,1):
            xml=f'<worksheet xmlns="{ns}"><sheetData>'
            for number,values in enumerate(rows,1):
                xml+=f'<row r="{number}">'
                for column,value in enumerate(values,1):
                    address=f'{chr(64+column)}{number}'
                    xml+=(f'<c r="{address}"><f>1080+5</f><v>1085</v></c>' if value.startswith('=') else
                          f'<c r="{address}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
                xml+='</row>'
            archive.writestr(f'xl/worksheets/sheet{index}.xml',xml+'</sheetData></worksheet>')
    return output.getvalue()


def test_native_table_xlsx_sheet_header_row_formula_and_cancel_navigation(page):
    prepare_table_import(page)
    upload_table(page,'ordinary-supplier.xlsx',table_xlsx())
    page.get_by_test_id('table-sheet').select_option('供应商报价')
    expect(page.get_by_test_id('table-header-row')).to_have_value('2')
    expect(page.get_by_test_id('table-map-unit_price')).to_have_value('E')
    page.get_by_test_id('table-data-row').select_option('4')
    expect(page.get_by_test_id('table-raw-grid')).to_contain_text('公式，不执行')
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('未知')
    expect(page.get_by_test_id('table-import-issues')).to_contain_text('FORMULA:unit_price')
    expect(page.get_by_test_id('table-result-tax_mode')).to_contain_text('未知')
    expect(page.get_by_test_id('table-result-shipping_cost')).to_contain_text('未知')
    page.get_by_test_id('table-data-row').select_option('3')
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('1080.50')
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('供应商报价!E3')
    expect(page.get_by_test_id('table-result-tax_mode')).to_contain_text('included')
    page.get_by_role('button',name='取消',exact=True).click()
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    page.get_by_test_id('open-table-import').click()
    expect(page.get_by_test_id('table-import-file')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    page.evaluate('history.pushState({}, "", "#table-import-navigation")')
    page.get_by_test_id('open-table-import').click()
    page.go_back()
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    page.go_forward()
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)


def test_native_table_cancelled_upload_ignores_late_result(page):
    prepare_table_import(page)
    pending=[]
    def delay(route):
        response=route.fetch()
        pending.append((route,response))
    page.route('**/api/v1/requests/*/table-imports',delay)
    page.get_by_test_id('open-table-import').click()
    page.get_by_test_id('table-import-file').set_input_files({'name':'cancelled.csv','mimeType':'text/csv','buffer':table_csv()})
    page.get_by_test_id('upload-table-preview').click()
    expect(page.get_by_test_id('table-import-working')).to_be_visible()
    wait_for_mock(page,lambda:len(pending)==1)
    page.get_by_label('关闭表格导入').click()
    page.get_by_test_id('open-table-import').click()
    route,response=pending.pop()
    try:route.fulfill(response=response)
    except Exception:pass  # Browser may have already disposed the aborted request.
    expect(page.get_by_test_id('table-import-file')).to_be_visible()
    expect(page.get_by_test_id('table-sheet')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    page.keyboard.press('Escape')


def test_native_table_revision_conflict_and_lost_confirmation_require_readback(page):
    prepare_table_import(page)
    upload_table(page,'recoverable.csv',table_csv())
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-mapped-preview')).to_be_visible()
    pending=[]
    def conflict(route):
        pending.append(route.request.post_data_json)
        route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':{
            'code':'VERSION_CONFLICT','message':'Synthetic stale revision'}}),
            headers={'Access-Control-Allow-Origin':URL})
    page.route('**/api/v1/table-imports/*/preview',conflict)
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-import-error')).to_contain_text('VERSION_CONFLICT')
    expect(page.get_by_test_id('preview-table-mapping')).to_be_disabled()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    assert len(pending)==1
    page.unroute('**/api/v1/table-imports/*/preview',conflict)
    page.get_by_test_id('read-table-import').click()
    expect(page.get_by_test_id('preview-table-mapping')).to_be_enabled()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-import-ack')).to_be_enabled()
    page.get_by_test_id('table-import-ack').check()
    confirmed=[]
    def lose_confirm(route):
        response=route.fetch();assert response.status==200
        confirmed.append(response.json()['id']);route.abort('failed')
    page.route('**/api/v1/table-imports/*/confirm',lose_confirm)
    page.get_by_test_id('confirm-table-import').click()
    expect(page.get_by_test_id('table-import-error')).to_contain_text('导入结果尚未核验')
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    assert len(confirmed)==1
    page.get_by_test_id('read-table-import').click()
    expect(page.get_by_test_id('table-import-success')).to_contain_text(confirmed[0])
    page.get_by_test_id('finish-table-import').click()
    expect(page.get_by_test_id('quote-row')).to_have_count(1)
    expect(page.get_by_test_id('confirm-quote')).to_be_enabled()
    assert len(confirmed)==1


# Multi-item gates use the disposable server's actual persistence and calculations.
# No response is mocked here, and no model or ERP execution control is invoked.
def prepare_multi_item(page, title='多物料浏览器验收'):
    page.goto(URL)
    page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('load-demo')).to_be_enabled()
    page.get_by_role('button', name='＋ 新建采购需求').click()
    form=page.get_by_test_id('request-form')
    form.locator('[name="title"]').fill(title)
    # Legacy scalar names remain until the buyer deliberately adds a line.
    expect(form.locator('[name="sku"]')).to_have_value('STAND-01')
    expect(form.locator('[name="quantity"]')).to_have_value('20')
    page.get_by_test_id('request-line-0-quantity').fill('2')
    page.get_by_test_id('add-request-line').click()
    page.get_by_test_id('request-line-1-sku').fill('CABLE-02')
    page.get_by_test_id('request-line-1-quantity').fill('3')
    with page.expect_response(lambda response: response.request.method=='POST' and response.url.endswith('/api/v1/requests')) as saved:
        form.get_by_role('button', name='保存需求').click()
    assert saved.value.status==201, saved.value.text()
    request=saved.value.json()
    expect(form).to_have_count(0)
    expect(page.get_by_test_id('request-lines')).to_contain_text('CABLE-02 · 3 EA')
    expect(page.get_by_test_id('open-table-import')).to_be_enabled()
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    return saved.value.url+'/'+request['id'], request


def multi_item_csv(*, partial=False):
    # Mixed tax modes, two absolute discounts and repeated quote-level freight.
    return ('Supplier ID,SKU,Quantity,Unit,Unit Price,Tax Mode,Tax Rate,Freight,Discount,Delivery Days,Currency\n'
            + ('SUP-MULTI,STAND-01,2,EA,,included,0.13,7.25,,7,CNY\n' if partial else
               'SUP-MULTI,STAND-01,2,EA,100.25,included,0.13,7.25,10.00,7,CNY\n')
            + 'SUP-MULTI,CABLE-02,3,EA,50.10,excluded,0.13,7.25,0.30,9,CNY\n').encode()


def preview_multi_item_table(page, *, rows=(2,3), partial=False):
    upload_table(page,'synthetic-multi-item.csv',multi_item_csv(partial=partial))
    expect(page.get_by_test_id('table-data-row')).to_have_count(0)
    expect(page.get_by_test_id('table-data-row-2')).to_be_checked()
    for number in (2,3):
        page.get_by_test_id(f'table-data-row-{number}').set_checked(number in rows)
    with page.expect_response(lambda response: response.request.method=='POST' and '/table-imports/' in response.url and response.url.endswith('/preview')) as saved:
        page.get_by_test_id('preview-table-mapping').click()
    assert saved.value.status==200, saved.value.text()
    expect(page.get_by_test_id('table-import-ack')).to_be_enabled()
    return saved.value.json()


def finish_multi_item_import(page):
    page.get_by_test_id('table-import-ack').check()
    with page.expect_response(lambda response: response.request.method=='POST' and '/table-imports/' in response.url and response.url.endswith('/confirm')) as saved:
        page.get_by_test_id('confirm-table-import').click()
    assert saved.value.status==200, saved.value.text()
    expect(page.get_by_test_id('table-import-success')).to_be_visible()
    page.get_by_test_id('finish-table-import').click()
    expect(page.get_by_test_id('quote-row')).to_have_count(1)
    expect(page.get_by_test_id('quote-row')).to_contain_text('待核对')
    return saved.value.json()


def read_multi_item_quotes(page, request_url):
    result=page.request.get(request_url+'/quotes',headers={'Authorization':'Bearer demo-buyer'})
    assert result.status==200, result.text()
    return result.json()


def confirm_multi_item_quote(page):
    page.get_by_test_id('confirm-quote').click()
    page.get_by_test_id('ack-confirm').check()
    with page.expect_response(lambda response: response.request.method=='POST' and '/api/v1/quotes/' in response.url and response.url.endswith('/confirm')) as saved:
        page.get_by_test_id('submit-confirm').click()
    assert saved.value.status==200, saved.value.text()
    expect(page.get_by_test_id('submit-confirm')).to_have_count(0)
    expect(page.get_by_test_id('confirm-quote')).to_be_disabled()
    return saved.value.json()


def test_native_multi_item_create_duplicate_and_twenty_line_boundary(page):
    page.goto(URL)
    page.get_by_test_id('login-demo-buyer').click()
    page.get_by_role('button',name='＋ 新建采购需求').click()
    form=page.get_by_test_id('request-form')
    expect(form.locator('[name="sku"]')).to_have_count(1)
    page.get_by_test_id('add-request-line').click()
    expect(form.locator('[name="sku"]')).to_have_count(0)
    expect(form.locator('[name="lines.0.sku"]')).to_have_value('STAND-01')
    page.get_by_test_id('request-line-1-sku').fill('STAND-01')
    expect(page.get_by_test_id('request-line-problems')).to_contain_text('重复型号 STAND-01')
    expect(form.get_by_role('button',name='保存需求')).to_be_disabled()
    page.get_by_test_id('request-line-1-sku').fill('CABLE-02')
    for index in range(2,20):
        page.get_by_test_id('add-request-line').click()
        page.get_by_test_id(f'request-line-{index}-sku').fill(f'ITEM-{index+1:02d}')
    expect(page.get_by_test_id('request-line')).to_have_count(20)
    expect(page.get_by_test_id('add-request-line')).to_be_disabled()
    expect(form.get_by_role('button',name='保存需求')).to_be_enabled()
    with page.expect_response(lambda response: response.request.method=='POST' and response.url.endswith('/api/v1/requests')) as saved:
        form.get_by_role('button',name='保存需求').click()
    assert saved.value.status==201, saved.value.text()
    command=saved.value.request.post_data_json
    assert len(command['lines'])==20 and 'sku' not in command and 'quantity' not in command
    assert len(saved.value.json()['lines'])==20
    expect(page.get_by_test_id('request-lines').locator('li')).to_have_count(20)
    # The server enforces the same boundary if a client bypasses disabled controls.
    headers={'Authorization':'Bearer demo-buyer'}
    too_many={**command,'lines':[*command['lines'],{'sku':'ITEM-21','quantity':'1','uom':'EA'}]}
    rejected=page.request.post(saved.value.url,headers=headers,data=too_many)
    assert rejected.status==422, rejected.text()
    duplicate={**command,'lines':[command['lines'][0],command['lines'][0]]}
    rejected=page.request.post(saved.value.url,headers=headers,data=duplicate)
    assert rejected.status==422, rejected.text()


def test_native_multi_item_csv_selected_rows_line_evidence_and_no_implicit_confirmation(page):
    request_url,_=prepare_multi_item(page)
    confirmations=[]
    page.on('request',lambda request:confirmations.append(request.url) if request.method=='POST' and '/api/v1/quotes/' in request.url and request.url.endswith('/confirm') else None)
    preview=preview_multi_item_table(page)
    assert preview['selection']['rows']==[2,3] and 'row' not in preview['selection']
    assert len(preview['values']['lines'])==2
    for index,price,cell in ((0,'100.25','E2'),(1,'50.10','E3')):
        result=page.get_by_test_id(f'table-result-lines.{index}.unit_price')
        expect(result).to_contain_text(price)
        expect(result).to_contain_text(cell)
        assert preview['evidence'][f'lines.{index}.unit_price']['cell_range']==cell
    expect(page.get_by_test_id('table-result-shipping_cost')).to_contain_text('H2')
    expect(page.get_by_test_id('table-result-shipping_cost')).to_contain_text('H3')
    page.get_by_test_id('table-import-ack').check()
    page.get_by_test_id('table-data-row-3').uncheck()
    expect(page.get_by_test_id('table-import-ack')).not_to_be_checked()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.get_by_test_id('table-data-row-3').check()
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-preview-dirty')).to_have_count(0)
    quote=finish_multi_item_import(page)
    assert quote['confirmed_by'] is None and len(quote['values']['lines'])==2
    persisted=read_multi_item_quotes(page,request_url)
    assert len(persisted)==1 and persisted[0]['confirmed_by'] is None
    assert persisted[0]['evidence']['lines.1.unit_price']['cell_range']=='E3'
    expect(page.get_by_test_id('quote-line-details').get_by_test_id('quote-line-detail')).to_have_count(2)
    page.get_by_test_id('line-0-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('E2')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('100.25')
    page.get_by_test_id('line-1-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('E3')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('50.10')
    assert confirmations==[]


def test_native_multi_item_partial_unknown_and_duplicate_correction_stays_unconfirmed(page):
    request_url,_=prepare_multi_item(page,'多物料缺失字段验收')
    preview=preview_multi_item_table(page,rows=(2,),partial=True)
    assert preview['values']['lines'][0]['unit_price'] is None
    expect(page.get_by_test_id('table-result-lines.0.unit_price')).to_contain_text('未知')
    quote=finish_multi_item_import(page)
    assert quote['calculation']['coverage']['missing_skus']==['CABLE-02']
    assert quote['calculation']['total'] is None and not quote['calculation']['eligible']
    # Explicit confirmation records review, but neither fills nulls nor repairs coverage.
    confirmed=confirm_multi_item_quote(page)
    assert confirmed['values']['lines'][0]['unit_price'] is None
    assert confirmed['values']['lines'][0]['discount'] is None
    assert confirmed['confirmed_by'] and not confirmed['calculation']['eligible']
    page.get_by_test_id('analyze').click()
    expect(page.get_by_test_id('request-status')).to_have_text('规则未通过')
    expect(page.get_by_test_id('evaluation-violations').first).to_contain_text('NO_ELIGIBLE_QUOTE')
    page.get_by_test_id('quote-row').get_by_role('button',name='修正字段').click()
    form=page.get_by_test_id('quote-form')
    expect(form.locator('[name="lines.0.unit_price"]')).to_have_value('')
    expect(form.locator('[name="lines.0.discount"]')).to_have_value('')
    expect(page.get_by_test_id('quote-coverage-problems')).to_contain_text('缺少需求型号：CABLE-02')
    page.get_by_test_id('add-quote-line').click()
    form.locator('[name="lines.1.sku"]').fill('STAND-01')
    form.locator('[name="reason"]').fill('Synthetic partial correction; unknown fields intentionally retained')
    expect(page.get_by_test_id('quote-coverage-problems')).to_contain_text('重复型号：STAND-01')
    expect(form.get_by_role('button',name='保存新版本')).to_be_disabled()
    form.locator('[name="lines.1.sku"]').fill('UNREQUESTED-03')
    expect(page.get_by_test_id('quote-coverage-problems')).to_contain_text('需求之外的型号：UNREQUESTED-03')
    expect(page.get_by_test_id('quote-coverage-problems')).to_contain_text('缺少需求型号：CABLE-02')
    form.locator('[name="lines.1.sku"]').fill('CABLE-02')
    form.locator('[name="lines.1.quantity"]').fill('3')
    form.locator('[name="lines.1.uom"]').fill('EA')
    # Saving an incomplete correction is allowed, but must create an unconfirmed version.
    with page.expect_response(lambda response: response.request.method=='PUT' and '/api/v1/quotes/' in response.url) as saved:
        form.get_by_role('button',name='保存新版本').click()
    assert saved.value.status==200, saved.value.text()
    command=saved.value.request.post_data_json['values']
    assert command['lines'][0]['unit_price'] is None and command['lines'][0]['discount'] is None
    assert command['lines'][1]['unit_price'] is None and command['lines'][1]['tax_mode']=='unknown'
    expect(form).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_contain_text('待核对')
    expect(page.get_by_test_id('confirm-quote')).to_be_enabled()
    current=read_multi_item_quotes(page,request_url)[0]
    assert current['version']==quote['version']+1 and current['confirmed_by'] is None
    assert current['values']['lines'][0]['unit_price'] is None
    assert current['values']['lines'][1]['tax_rate'] is None
    assert current['calculation']['total'] is None and not current['calculation']['eligible']
    expect(page.get_by_test_id('execute')).to_have_count(0)


def test_native_multi_item_deterministic_line_totals_discount_tax_and_freight_once(page):
    request_url,_=prepare_multi_item(page,'多物料金額验收')
    preview_multi_item_table(page)
    quote=finish_multi_item_import(page)
    calculation=quote['calculation']
    assert [(line['goods'],line['discount'],line['added_tax'],line['total']) for line in calculation['lines']]==[
        ('200.50','10.00','0.00','190.50'),('150.30','0.30','19.50','169.50')]
    assert {key:calculation[key] for key in ('goods','discount','added_tax','shipping','total')}=={
        'goods':'350.80','discount':'10.30','added_tax':'19.50','shipping':'7.25','total':'367.25'}
    detail=page.get_by_test_id('quote-line-details')
    expect(detail.get_by_test_id('quote-line-detail').nth(0)).to_contain_text('190.50')
    expect(detail.get_by_test_id('quote-line-detail').nth(1)).to_contain_text('169.50')
    expect(detail.get_by_test_id('quote-cost-totals')).to_have_text(
        '商品金额 350.80 · 商品折扣 10.30 · 新增税额 19.50 · 整单含税运费 7.25 · 总价 367.25 CNY')
    confirmed=confirm_multi_item_quote(page)
    assert confirmed['calculation']['eligible']
    page.get_by_test_id('analyze').click()
    expect(page.get_by_test_id('proposal-line-details').get_by_test_id('quote-line-detail')).to_have_count(2)
    expect(page.get_by_test_id('proposal-line-details').get_by_test_id('quote-cost-totals')).to_contain_text('总价 367.25 CNY')
    expect(page.get_by_test_id('multi-erp-boundary')).to_contain_text('真实 ERPNext 草稿当前仅支持')
    saved=page.request.get(request_url,headers={'Authorization':'Bearer demo-buyer'}).json()
    assert saved['proposal']['total']=='367.25'
    assert saved['proposal']['quote_values']['lines']==confirmed['values']['lines']
    assert saved['proposal']['quote_collection'][0]['calculation']['shipping']=='7.25'
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output=os.getenv('PF_SCREENSHOT_DIR')
    if output:page.screenshot(path=str(Path(output)/'next-multi-item-mobile.png'),full_page=True)


def test_native_multi_item_request_line_edit_stales_approval_and_preserves_snapshot(page):
    request_url,_=prepare_multi_item(page,'多物料审批失效验收')
    preview_multi_item_table(page)
    finish_multi_item_import(page)
    confirm_multi_item_quote(page)
    page.get_by_test_id('analyze').click()
    expect(page.get_by_test_id('proposal-line-details')).to_be_visible()
    approve(page)
    before=page.request.get(request_url,headers={'Authorization':'Bearer demo-buyer'}).json()
    page.get_by_test_id('edit-request').click()
    form=page.get_by_test_id('request-form')
    expect(form.locator('[name="quantity"]')).to_have_count(0)
    form.locator('[name="lines.1.quantity"]').fill('4')
    form.get_by_role('button',name='保存需求').click()
    expect(form).to_have_count(0)
    expect(page.get_by_test_id('request-status')).to_have_text('审批已失效')
    expect(page.get_by_test_id('request-lines')).to_contain_text('CABLE-02 · 4 EA')
    # Request edits clear the active proposal; only the immutable approval
    # history retains the old snapshot. Policy changes may retain a stale
    # proposal, but a request edit must not leave approval controls attached.
    expect(page.get_by_test_id('proposal-line-details')).to_have_count(0)
    expect(page.get_by_test_id('proposal-body')).to_contain_text('需要重新生成和审批')
    current=page.request.get(request_url,headers={'Authorization':'Bearer demo-buyer'}).json()
    assert current['proposal'] is None and current['proposal_current'] is False
    assert current['lines'][1]['quantity']=='4'
    expect(page.get_by_test_id('execute')).to_have_count(0)
    page.get_by_test_id('approval-history').locator('summary').first.click()
    expect(page.get_by_test_id('approval-current').first).to_have_text('已失效，仅供审计')
    history=page.request.get(request_url+'/approvals',headers={'Authorization':'Bearer demo-buyer'}).json()
    assert not history[0]['current']
    assert history[0]['snapshot_hash']==before['proposal']['snapshot_hash']
    assert history[0]['snapshot']['request']['lines'][1]['quantity']=='3'
    page.get_by_label('切换演示身份').select_option('demo-approver')
    expect(page.get_by_test_id('identity')).to_have_text('approver')
    expect(page.get_by_test_id('approve')).to_have_count(0)
    expect(page.get_by_test_id('reject')).to_have_count(0)


def test_native_multi_item_role_cancel_back_and_mobile_dialog_guards(page):
    request_url,request=prepare_multi_item(page,'多物料编辑器边界验收')
    preview_multi_item_table(page)
    finish_multi_item_import(page)
    writes=[]
    page.on('request',lambda value:writes.append((value.method,value.url)) if value.method in ('PUT','POST') and '/api/v1/' in value.url else None)
    page.set_viewport_size({'width':390,'height':844})
    page.get_by_test_id('edit-request').click()
    page.get_by_test_id('add-request-line').click()
    page.get_by_test_id('request-line-2-sku').fill('UNSAVED-REQUEST')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_test_id('request-form').get_by_role('button',name='取消',exact=True).click()
    page.get_by_test_id('edit-request').click()
    expect(page.get_by_test_id('request-line')).to_have_count(2)
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('request-form')).to_have_count(0)
    page.get_by_test_id('quote-row').get_by_role('button',name='修正字段').click()
    form=page.get_by_test_id('quote-form')
    form.locator('[name="lines.0.unit_price"]').fill('999.00')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    form.get_by_role('button',name='取消',exact=True).click()
    page.get_by_test_id('quote-row').get_by_role('button',name='修正字段').click()
    expect(form.locator('[name="lines.0.unit_price"]')).to_have_value('100.25')
    # Reindexing an unsaved row must preserve its original source, rather than
    # accidentally revealing the deleted row's price evidence.
    form.get_by_role('button',name='删除报价物料 1',exact=True).click()
    expect(form.locator('[name="lines.0.sku"]')).to_have_value('CABLE-02')
    form.get_by_role('button',name='查看物料 1 单价来源',exact=True).click()
    expect(page.get_by_test_id('quote-editor-evidence')).to_contain_text('E3')
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('E3')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('50.10')
    page.get_by_test_id('quote-row').get_by_role('button',name='修正字段').click()
    form.get_by_role('button',name='删除报价物料 1',exact=True).click()
    page.get_by_test_id('add-quote-line').click()
    expect(form.locator('[name="lines.1.unit_price"]')).to_have_value('')
    form.get_by_role('button',name='查看物料 2 单价来源',exact=True).click()
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('evidence-body')).to_contain_text('新增物料尚无已保存的来源')
    expect(page.get_by_test_id('evidence-body')).not_to_contain_text('100.25')
    expect(page.get_by_test_id('evidence-body')).not_to_contain_text('50.10')
    page.evaluate('history.pushState({}, "", "#multi-item-navigation")')
    page.get_by_test_id('quote-row').get_by_role('button',name='修正字段').click()
    page.get_by_test_id('add-quote-line').click()
    page.go_back()
    expect(form).to_have_count(0)
    expect(page.get_by_test_id('login-demo-buyer')).to_be_visible()
    page.go_forward()
    expect(form).to_have_count(0)
    expect(page.get_by_test_id('login-demo-buyer')).to_be_visible()
    page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('open-table-import')).to_be_enabled()
    page.get_by_test_id('open-table-import').click()
    expect(page.get_by_test_id('table-import-dialog')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    page.set_viewport_size({'width':1440,'height':1100})
    page.get_by_label('切换演示身份').select_option('demo-approver')
    expect(page.get_by_test_id('identity')).to_have_text('approver')
    for test_id in ('edit-request','open-table-import','analyze','confirm-quote'):
        expect(page.get_by_test_id(test_id)).to_be_disabled()
    expect(page.get_by_role('button',name='＋ 新建采购需求')).to_be_disabled()
    expect(page.get_by_test_id('quote-row').get_by_role('button',name='修正字段')).to_be_disabled()
    assert writes==[], writes
    saved=page.request.get(request_url,headers={'Authorization':'Bearer demo-buyer'}).json()
    assert saved['lines']==request['lines']
    quote=read_multi_item_quotes(page,request_url)[0]
    assert quote['version']==1 and quote['confirmed_by'] is None
    assert len(quote['values']['lines'])==2 and quote['values']['lines'][0]['unit_price']=='100.25'
