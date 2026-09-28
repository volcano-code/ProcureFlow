"""Real Chromium + real HTTP + disposable database, not jsdom or API-only E2E."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]

@pytest.fixture
def live_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PF_DATA_DIR': str(tmp_path), 'PF_DATABASE_URL': f'sqlite:///{tmp_path / "browser.sqlite3"}',
           'PF_MODE': 'demo', 'PF_ERP_MODE': 'mock'}
    env.pop('PF_AUTH_TOKENS', None)
    env['ERP_ALLOW_DRAFT_WRITES'] = 'false'
    for key in ('ERP_API_KEY', 'ERP_API_SECRET', 'ERP_BASE_URL', 'ERP_COMPANY', 'LLM_API_KEY'):
        env.pop(key, None)
    log = (tmp_path / 'server.log').open('w')
    process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/start.py'), '--port', str(port)],
                               cwd=ROOT, env=env, stdout=log, stderr=log)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                if httpx.get(url + '/health', timeout=0.3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError('API failed to start: ' + (tmp_path / 'server.log').read_text())
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()

@pytest.fixture
def browser_page():
    pw = pytest.importorskip('playwright.sync_api')
    executable = os.getenv('PF_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    with pw.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(executable_path=executable, headless=True,
                                                 args=['--no-sandbox', '--disable-dev-shm-usage'])
        except pw.Error as error:
            if os.getenv('PF_REQUIRE_BROWSER') == '1':
                raise
            pytest.skip(f'Install Chromium using python -m playwright install chromium: {error}')
        page = browser.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert not errors, errors


def prepare(page, url):
    try:
        page.goto(url)
    except Exception as error:
        if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(error) and os.getenv('PF_REQUIRE_BROWSER') != '1':
            pytest.skip('ENVIRONMENT_BLOCKED: Chromium policy prohibits navigation to the local test server; browser E2E NOT verified')
        raise
    page.locator('#load-demo').click()
    page.wait_for_function("document.querySelectorAll('#quote-body tr').length === 3")
    assert '24800.00' in page.locator('#quote-body').inner_text()
    assert '24200.00' in page.locator('#quote-body').inner_text()
    assert '未知' in page.locator('#quote-body').inner_text()
    for index in range(3):
        page.locator('[data-confirm]').nth(index).click()
        page.locator('#ack-confirm').click()
        page.locator('#modal').wait_for(state='hidden')
        page.wait_for_function(f"document.querySelectorAll('[data-confirm]')[{index}].disabled")
    page.locator('#analyze').click()
    page.wait_for_function("document.querySelector('#proposal-body').textContent.includes('24200.00')")


@pytest.mark.browser
def test_browser_end_to_end_with_evidence_and_persistence(live_server, browser_page):
    page = browser_page
    prepare(page, live_server)
    page.locator('#quote-body tr').filter(has_text='SUP-C').locator('[data-field="unit_price"]').click()
    page.wait_for_function("document.querySelector('#evidence-body').textContent.includes('第 1 页')")
    assert '1180.00' in page.locator('#evidence-body').inner_text()
    page.locator('#role').select_option('demo-approver')
    page.locator('#approve').click()
    page.wait_for_function("document.querySelector('#request-status').textContent.includes('已批准')")
    page.locator('#role').select_option('demo-buyer')
    page.locator('#execute').click()
    page.wait_for_function("document.querySelector('#proposal-body').textContent.includes('MOCK-SQ-')")
    assert '草稿已创建' in page.locator('#request-status').inner_text()
    assert 'COMPLETED' in page.locator('#proposal-body').inner_text()
    page.reload()
    page.wait_for_function("document.querySelector('#proposal-body').textContent.includes('MOCK-SQ-')")
    page.locator('#quote-body tr').filter(has_text='SUP-C').locator('[data-field="unit_price"]').click()
    page.wait_for_function("document.querySelector('#evidence-body').textContent.includes('第 1 页')")
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    report_dir = os.getenv('PF_SCREENSHOT_DIR')
    if report_dir:
        out = Path(report_dir)
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / 'workbench.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
    if report_dir:
        page.screenshot(path=str(out / 'workbench-mobile.png'), full_page=True)


@pytest.mark.browser
def test_browser_edit_after_approval_invalidates_snapshot(live_server, browser_page):
    page = browser_page
    prepare(page, live_server)
    page.locator('#role').select_option('demo-approver')
    page.locator('#approve').click()
    page.wait_for_function("document.querySelector('#request-status').textContent.includes('已批准')")
    page.locator('#role').select_option('demo-buyer')
    page.locator('#edit-request').click()
    page.locator('#request-form input[name="quantity"]').fill('21')
    page.locator('#request-form button[type="submit"]').click()
    page.locator('#modal').wait_for(state='hidden')
    page.wait_for_function("document.querySelector('#request-status').textContent.includes('审批已失效')")
    assert page.locator('#execute').count() == 0
    assert '21 EA' in page.locator('#request-meta').inner_text()
