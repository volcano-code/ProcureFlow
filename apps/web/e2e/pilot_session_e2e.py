"""Native Next + real disposable pilot API. No auth endpoint is mocked.

Operator-only provisioning/revocation updates the same disposable database. Network
interception is limited to loss/delay cases after actual server authentication.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import expect
from sqlalchemy import select
from sqlalchemy.engine import make_url
from workbench_e2e import page, URL, wait_for_mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/api'))
from procureflow.auth import IdentityService
from procureflow.config import Settings
from procureflow.db import Database, PilotSessionRow

API = os.environ.get('NEXT_PUBLIC_API_BASE_URL', '')
if API.startswith('/'):
    API = URL.rstrip('/') + API
if os.environ.get('PF_BROWSER_TRACE_DIR'):
    raise RuntimeError('Pilot credentials must not be recorded in browser traces')
if os.environ.get('PF_MODE') != 'pilot' or os.environ.get('PF_ALLOW_TEST_MUTATIONS') != '1':
    raise RuntimeError('Use scripts/verify_next.py --pilot with its disposable migrated API')
db_url = make_url(os.environ.get('PF_DATABASE_URL', 'sqlite://'))
data_dir = Path(os.environ.get('PF_DATA_DIR', '')).resolve()
if (urlparse(API).hostname not in {'127.0.0.1', 'localhost'} or db_url.get_backend_name() != 'sqlite'
        or not data_dir.name.startswith('pf-next-e2e-')
        or Path(db_url.database or '').resolve() != data_dir / 'next.sqlite3'):
    raise RuntimeError('Pilot UI fixtures require the gate-owned temporary SQLite database and loopback API')


class Operator:
    def __init__(self):
        self.settings = Settings()
        self.db = Database(self.settings.database_url)
        self.auth = IdentityService(self.db, self.settings)
        suffix = uuid4().hex[:10]
        self.tenants = {'a': 'browser-a-' + suffix, 'b': 'browser-b-' + suffix}
        for tenant in self.tenants.values():
            self.auth.create_tenant(tenant)
            for user, role in [('buyer', 'buyer'), ('approver', 'approver'), ('auditor', 'auditor')]:
                self.auth.set_membership(tenant, user, role)

    def invite(self, user='buyer', tenant='a'):
        return self.auth.issue_invite(self.tenants[tenant], user)['credential']

    def revoke(self, user='buyer', tenant='a'):
        self.auth.revoke_user_sessions(self.tenants[tenant], user)

    def expire(self, user='buyer', tenant='a'):
        with self.db.transaction(write=True) as session:
            rows = session.scalars(select(PilotSessionRow).where(
                PilotSessionRow.tenant_id == self.tenants[tenant], PilotSessionRow.user_id == user))
            for row in rows:
                row.expires_at = '2000-01-01T00:00:00+00:00'


@pytest.fixture
def operator():
    health = httpx.get(API + '/health', timeout=5).json()
    assert health['mode'] == 'pilot' and health['erp'] == 'mock'
    value = Operator()
    try:
        yield value
    finally:
        value.db.engine.dispose()


def sign_in(p, operator, user='buyer', tenant='a', credential=None):
    if not p.url.startswith(URL):
        p.goto(URL)
    expect(p.get_by_test_id('login-credential')).to_be_enabled()
    p.get_by_test_id('login-credential').fill(credential or operator.invite(user, tenant))
    with p.expect_response(lambda r: r.url.endswith('/api/v1/auth/login') and r.request.method == 'POST') as exchange:
        p.get_by_test_id('login-submit').click()
    result = exchange.value.json()
    assert exchange.value.status == 200
    expect(p.get_by_test_id('identity')).to_have_text(user)
    expect(p.get_by_test_id('identity-user')).to_contain_text(user)
    expect(p.get_by_test_id('identity-tenant')).to_contain_text(operator.tenants[tenant])
    expect(p.get_by_test_id('session-expiry')).to_contain_text('到期')
    return result


def create_request(p, title='Synthetic pilot browser request'):
    p.get_by_role('button', name='＋ 新建采购需求', exact=True).click()
    p.get_by_test_id('request-form').locator('[name="title"]').fill(title)
    p.get_by_test_id('request-form').get_by_role('button', name='保存需求').click()
    expect(p.get_by_test_id('request-form')).to_have_count(0)
    expect(p.get_by_test_id('request-meta')).to_be_visible()
    expect(p.get_by_test_id('stream-status')).to_contain_text('live')


def assert_cleared(p):
    expect(p.get_by_test_id('identity')).to_have_text('未登录')
    for test_id in ['request-meta', 'quote-row', 'evidence-body', 'proposal-body', 'policy-panel',
                    'advice-panel', 'table-import-dialog', 'request-form', 'stream-status']:
        expect(p.get_by_test_id(test_id)).to_have_count(0)
    expect(p.locator('dialog')).to_have_count(0)


def test_pilot_two_independent_contexts_logout_and_tenant_isolation(page, operator):
    other_context = page.context.browser.new_context()
    other = other_context.new_page()
    try:
        urls = []
        page.on('request', lambda r: urls.append(r.url))
        buyer = sign_in(page, operator)
        expect(page.get_by_test_id('login-demo-buyer')).to_have_count(0)
        expect(page.get_by_label('切换演示身份')).to_have_count(0)
        create_request(page, 'Tenant A private synthetic request')
        approver = sign_in(other, operator, 'approver')
        expect(other.get_by_test_id('request-meta')).to_be_visible()
        expect(other.get_by_role('button', name='＋ 新建采购需求')).to_be_disabled()
        browser_state = page.evaluate('''() => ({local:JSON.stringify(localStorage), session:JSON.stringify(sessionStorage),
            cookies:document.cookie, html:document.body.innerHTML, history:location.href})''')
        assert all(buyer['token'] not in value for value in browser_state.values())
        assert all(buyer['token'] not in url for url in urls)
        assert page.context.cookies() == []
        page.get_by_test_id('logout').click()
        assert_cleared(page)
        expect(page.get_by_test_id('session-notice')).to_contain_text('已退出，会话已在服务端撤销')
        assert httpx.get(API + '/api/v1/me', headers={'Authorization': 'Bearer ' + buyer['token']}).status_code == 401
        assert httpx.get(API + '/api/v1/me', headers={'Authorization': 'Bearer ' + approver['token']}).status_code == 200
        expect(other.get_by_test_id('identity')).to_have_text('approver')
        sign_in(page, operator, tenant='b')
        expect(page.get_by_test_id('request-meta')).to_have_count(0)
        expect(page.locator('body')).not_to_contain_text('Tenant A private synthetic request')
        page.reload()
        assert_cleared(page)
    finally:
        other_context.close()


def test_pilot_revocation_closes_open_nested_dialog_and_stops_sse(page, operator):
    streams = []
    page.on('request', lambda r: streams.append(r.url) if '/events/stream' in r.url else None)
    session = sign_in(page, operator)
    create_request(page)
    page.get_by_test_id('open-table-import').click()
    expect(page.get_by_test_id('table-import-dialog')).to_be_visible()
    operator.revoke()
    assert_cleared(page)
    expect(page.get_by_test_id('session-notice')).to_contain_text('失效或被撤销')
    count = len(streams)
    page.wait_for_timeout(1800)
    assert len(streams) == count
    assert httpx.get(API + '/api/v1/me', headers={'Authorization': 'Bearer ' + session['token']}).status_code == 401
    sign_in(page, operator)
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)


def test_pilot_expiry_without_request_and_server_expiry(page, operator):
    page.clock.install()
    session = sign_in(page, operator)
    # Browser clock acceleration verifies the independently scheduled local expiry boundary.
    page.clock.fast_forward(3_600_001)
    assert_cleared(page)
    expect(page.get_by_test_id('session-notice')).to_contain_text('已过期')
    operator.expire()
    assert httpx.get(API + '/api/v1/me', headers={'Authorization': 'Bearer ' + session['token']}).status_code == 401


def test_pilot_real_expired_session_terminates_live_stream(page, operator):
    sign_in(page, operator)
    create_request(page)
    operator.expire()
    assert_cleared(page)
    expect(page.get_by_test_id('session-notice')).to_contain_text('失效或被撤销')


def test_pilot_global_401_from_child_read_clears_evidence(page, operator):
    sign_in(page, operator)
    create_request(page)
    page.locator('input[type="file"]').first.set_input_files(str(ROOT / 'apps/demo/samples/supplier-a.txt'))
    expect(page.get_by_test_id('quote-row')).to_have_count(1)
    page.get_by_test_id('field-unit_price').click()
    expect(page.get_by_test_id('evidence-body')).to_contain_text('1200')
    # Revoke when the child read is already dispatched; no auth response is mocked.
    def revoke_at_read(route):
        operator.revoke()
        route.continue_()
    page.route('**/api/v1/policy', revoke_at_read)
    page.get_by_test_id('refresh-policy').click()
    assert_cleared(page)


def test_pilot_cancelled_login_cannot_repaint_after_new_identity(page, operator):
    page.goto(URL)
    pending = []
    def hold(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        response = route.fetch()
        assert response.status == 200
        pending.append((route, response))
    page.route('**/api/v1/auth/login', hold)
    page.get_by_test_id('login-credential').fill(operator.invite())
    page.get_by_test_id('login-submit').click()
    wait_for_mock(page, lambda: len(pending) == 1)
    page.get_by_test_id('cancel-login').click()
    assert_cleared(page)
    page.unroute('**/api/v1/auth/login', hold)
    sign_in(page, operator, tenant='b')
    route, response = pending.pop()
    try:
        route.fulfill(response=response)
    except Exception:
        pass  # Chromium may already have discarded the explicitly aborted request.
    expect(page.get_by_test_id('identity-tenant')).to_contain_text(operator.tenants['b'])
    expect(page.get_by_test_id('request-meta')).to_have_count(0)


def test_pilot_logout_pending_mutation_cannot_repaint_new_tenant(page, operator):
    sign_in(page, operator)
    pending = []
    def hold(route):
        if route.request.method == 'POST':
            response = route.fetch()
            pending.append((route, response))
        else:
            route.continue_()
    page.route('**/api/v1/requests', hold)
    page.get_by_role('button', name='＋ 新建采购需求', exact=True).click()
    page.get_by_test_id('request-form').locator('[name="title"]').fill('Late tenant A mutation')
    page.get_by_test_id('request-form').get_by_role('button', name='保存需求').click()
    wait_for_mock(page, lambda: len(pending) == 1)
    page.get_by_test_id('logout').click()
    assert_cleared(page)
    sign_in(page, operator, tenant='b')
    route, response = pending.pop()
    try:
        route.fulfill(response=response)
    except Exception:
        pass
    expect(page.get_by_test_id('identity-tenant')).to_contain_text(operator.tenants['b'])
    expect(page.get_by_test_id('request-meta')).to_have_count(0)
    expect(page.locator('body')).not_to_contain_text('Late tenant A mutation')
    page.wait_for_timeout(300)
    assert pending == []  # No automatic retry or follow-on mutation.


def test_pilot_logout_network_failure_is_truthful_and_local_clear_is_immediate(page, operator):
    session = sign_in(page, operator)
    create_request(page)
    page.route('**/api/v1/auth/logout', lambda route: route.abort('failed'))
    page.get_by_test_id('logout').click()
    assert_cleared(page)
    expect(page.get_by_test_id('session-notice')).to_contain_text('无法确认服务端撤销')
    assert httpx.get(API + '/api/v1/me', headers={'Authorization': 'Bearer ' + session['token']}).status_code == 200
    operator.revoke()


def test_pilot_one_use_failure_repeated_login_and_history_never_restore(page, operator):
    invitation = operator.invite()
    sign_in(page, operator, credential=invitation)
    create_request(page)
    page.evaluate('history.pushState({}, "", "#synthetic-history")')
    page.go_back()
    assert_cleared(page)
    page.go_forward()
    assert_cleared(page)
    page.get_by_test_id('login-credential').fill(invitation)
    page.get_by_test_id('login-submit').click()
    expect(page.get_by_test_id('session-notice')).to_contain_text('登录失败')
    assert_cleared(page)
    expect(page.get_by_test_id('login-credential')).to_have_value('')
    sign_in(page, operator)
    page.get_by_test_id('logout').click()
    assert_cleared(page)
    sign_in(page, operator)
    expect(page.get_by_test_id('request-meta')).to_be_visible()
    page.goto('about:blank')
    page.go_back()
    assert_cleared(page)
