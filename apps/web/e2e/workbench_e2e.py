"""Strict native Next.js browser gates. No fallback to apps/demo, no skips."""
import os
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
