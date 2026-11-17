"""Native recovery UI: disposable real API integration plus explicit browser fault injection.

Run against the same loopback temporary mock API boundary as verify_next.py.
Mocked cases test frontend cancellation/denial; they do not certify live ERP behavior.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, Error
from workbench_e2e import page, prepare, approve, URL


def disposable_database(page):
    from sqlalchemy.engine import make_url
    from procureflow.db import Database
    api = os.environ.get('NEXT_PUBLIC_API_BASE_URL', '').rstrip('/')
    database_url = os.environ.get('PF_DATABASE_URL', 'sqlite://')
    parsed = make_url(database_url)
    data = Path(os.environ.get('PF_DATA_DIR', '')).resolve()
    if (os.environ.get('PF_ALLOW_TEST_MUTATIONS') != '1' or os.environ.get('PF_MODE') != 'demo'
            or os.environ.get('PF_ERP_MODE') != 'mock'
            or urlparse(api).hostname not in {'localhost', '127.0.0.1'}
            or parsed.get_backend_name() != 'sqlite' or not data.name.startswith('pf-next-e2e-')
            or Path(parsed.database or '').resolve() != data / 'next.sqlite3'
            or not (data / 'next.sqlite3').is_file()):
        raise RuntimeError('Recovery fixture requires gate-owned temporary SQLite and loopback mock API')
    health = page.request.get(api + '/health')
    assert health.status == 200 and health.json()['mode'] == 'demo' and health.json()['erp'] == 'mock'
    return Database(database_url), data, api


def all_rows(db):
    from sqlalchemy import select
    from procureflow.db import Base
    with db.transaction() as session:
        return {table.name: [dict(row) for row in session.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                for table in Base.metadata.sorted_tables}


def test_native_recovery_real_held_operation_remains_read_only(page):
    """Synthetic successful mock operation is held locally; UI cannot release or replay it."""
    from procureflow.db import RecoveryHoldRow, OperationRow, now
    prepare(page)
    approve(page)
    with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/execute')) as execution:
        page.get_by_test_id('execute').click()
    operation_id = execution.value.json()['id']
    expect(page.get_by_test_id('request-status')).to_have_text('草稿已创建')
    db, data, api = disposable_database(page)
    try:
        with db.transaction(write=True) as session:
            operation = session.get(OperationRow, operation_id)
            assert operation and operation.tenant_id == 'demo'
            session.add(RecoveryHoldRow(operation_id=operation_id, restore_id='synthetic-browser-hold',
                                       original_status=operation.status, created_at=now()))
        before = all_rows(db)
        requests = []
        page.on('request', lambda request: requests.append((request.method, request.url))
                if '/api/v1/recovery/' in request.url else None)
        page.get_by_test_id('refresh-recovery').click()
        row = page.get_by_test_id('recovery-operation').filter(has_text=operation_id)
        expect(row).to_contain_text('保持隔离')
        row.get_by_test_id('open-recovery-detail').click()
        expect(page.get_by_test_id('recovery-source').first).to_contain_text('原始文件完整性已核验')
        expect(page.get_by_test_id('recovery-approval')).to_be_visible()
        page.get_by_test_id('reconcile-recovery').click()
        expect(page.get_by_test_id('recovery-result-status')).to_contain_text('快照与远端草稿一致')
        expect(page.get_by_test_id('recovery-reconciliation-result')).to_contain_text('模拟 ERP 结果，未验证真实 ERP')
        assert all_rows(db) == before
        assert requests and all(method == 'GET' for method, _ in requests)
        assert page.get_by_test_id('recovery-panel').get_by_role('button', name='解除隔离').count() == 0
        output = os.getenv('PF_SCREENSHOT_DIR')
        if output:
            Path(output).mkdir(parents=True, exist_ok=True)
            page.get_by_test_id('recovery-panel').screenshot(path=str(Path(output) / 'recovery-read-only-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if output:
            page.get_by_test_id('recovery-panel').screenshot(path=str(Path(output) / 'recovery-read-only-mobile.png'))
    finally:
        db.engine.dispose()


def operation(identifier):
    return {'id': identifier, 'request_id': 'request-' + identifier, 'request_title': '合成隔离需求 ' + identifier,
            'status': 'RECONCILING', 'attempts': 1, 'remote_id': None, 'error': None, 'snapshot_hash': 'snapshot-' + identifier,
            'created_at': '2026-10-08T12:00:00Z', 'outbox_status': 'HELD', 'replay_permitted': False,
            'hold': {'restore_id': 'synthetic-restore', 'original_status': 'IN_FLIGHT', 'created_at': '2026-10-08T12:01:00Z'}}


def detail(identifier):
    return {**operation(identifier), 'payload_sha256': 'payload-' + identifier, 'ledger_sha256': 'ledger-' + identifier,
            'expected': {'quantity': '20', 'docstatus': 0}, 'approval': None,
            'sources': [{'id': None, 'filename': '缺失的合成来源.csv', 'sha256': None, 'integrity': 'unavailable',
                         'quote_id': None, 'quote_version': None}], 'diagnostics': ['APPROVAL_MISSING', 'SOURCE_UNAVAILABLE']}


def receipt(identifier, status='missing', **extra):
    return {'operation_id': identifier, 'ledger_sha256': 'ledger-' + identifier, 'verified_at': '2026-10-08T12:02:00Z',
            'status': status, 'simulated': True, 'network_attempted': False, 'external_write_attempted': False,
            'replay_permitted': False, 'remote_id': None, 'expected': {'quantity': '20', 'docstatus': 0},
            'observed': None, 'differences': [], 'matches_snapshot': False, 'draft_verified': False, **extra}


def fulfill(route, body, status=200):
    route.fulfill(status=status, content_type='application/json', body=json.dumps(body))


def mock_workspace(page, reconciliation, detail_handler=None):
    """Only recovery/requests are mocked; login boundary is the real native app."""
    calls = []
    page.route('**/api/v1/requests', lambda route: fulfill(route, []))
    def route_recovery(route):
        request = route.request
        calls.append((request.method, request.url))
        path = urlparse(request.url).path
        if path.endswith('/operations'):
            second = 'after=' in request.url
            fulfill(route, {'state': {'state': 'ACTIVE', 'generation': 2, 'required_auth_mode': 'demo', 'restore_id': 'synthetic'},
                            'items': [operation('op3')] if second else [operation('op1'), operation('op2')],
                            'next_after': None if second else 'op2', 'total': 3})
        elif path.endswith('/reconciliation'):
            reconciliation(route, path.split('/')[-2])
        elif detail_handler:
            detail_handler(route, path.split('/')[-1])
        else:
            fulfill(route, detail(path.split('/')[-1]))
    page.route('**/api/v1/recovery/operations**', route_recovery)
    page.goto(URL)
    expect(page.get_by_test_id('recovery-panel')).to_have_count(0)
    page.get_by_test_id('login-demo-buyer').click()
    expect(page.get_by_test_id('recovery-operation')).to_have_count(2)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    return calls


def release_late(route, result):
    try:
        fulfill(route, result)
    except Error:
        # Abort may already have reached Chromium. Both cancellation and ignored-abort
        # late arrival must leave the newer UI scope intact (unit tests cover ignored fetch).
        pass


def test_native_recovery_repeated_click_close_cancel_pagination_and_new_identity(page):
    pending = []
    calls = mock_workspace(page, lambda route, identifier: pending.append((route, identifier)))
    page.get_by_test_id('open-recovery-detail').first.click()
    expect(page.get_by_test_id('recovery-source')).to_contain_text('无法核验原始文件')
    page.get_by_test_id('reconcile-recovery').evaluate('(button) => { button.click(); button.click(); }')
    expect(page.get_by_test_id('cancel-recovery-reconciliation')).to_be_visible()
    assert len([item for item in calls if item[1].endswith('/reconciliation')]) == 1
    page.get_by_test_id('close-recovery-detail').click()
    page.get_by_test_id('open-recovery-detail').nth(1).click()
    expect(page.get_by_test_id('recovery-ledger-hash')).to_have_text('ledger-op2')
    release_late(pending[0][0], receipt('op1', reason='STALE_RESPONSE_MUST_NOT_APPEAR'))
    expect(page.get_by_test_id('recovery-reconciliation-result')).to_have_count(0)
    expect(page.get_by_test_id('recovery-detail')).not_to_contain_text('STALE_RESPONSE_MUST_NOT_APPEAR')
    page.get_by_test_id('reconcile-recovery').click()
    page.get_by_test_id('cancel-recovery-reconciliation').click()
    release_late(pending[1][0], receipt('op2'))
    expect(page.get_by_test_id('recovery-detail')).to_contain_text('已取消本页等待')
    expect(page.get_by_test_id('recovery-reconciliation-result')).to_have_count(0)
    page.get_by_test_id('reconcile-recovery').click()
    page.get_by_test_id('recovery-next-page').click()
    expect(page.get_by_test_id('recovery-operation')).to_have_count(1)
    expect(page.get_by_test_id('recovery-detail')).to_have_count(0)
    release_late(pending[2][0], receipt('op2'))
    page.get_by_test_id('open-recovery-detail').click()
    expect(page.get_by_test_id('recovery-ledger-hash')).to_have_text('ledger-op3')
    page.get_by_test_id('reconcile-recovery').click()
    page.get_by_label('切换演示身份').select_option('demo-auditor')
    expect(page.get_by_test_id('identity')).to_have_text('auditor')
    release_late(pending[3][0], receipt('op3'))
    expect(page.get_by_test_id('recovery-detail')).to_have_count(0)
    expect(page.get_by_test_id('recovery-reconciliation-result')).to_have_count(0)
    assert all(method == 'GET' for method, _ in calls)
    page.evaluate("history.pushState({}, '', '?recovery-test'); window.dispatchEvent(new PopStateEvent('popstate'))")
    expect(page.get_by_test_id('recovery-panel')).to_have_count(0)
    expect(page.get_by_test_id('login-demo-buyer')).to_be_visible()


def test_native_recovery_uncertainty_binding_failure_and_auth_denials(page):
    current = {'status': 'missing', 'http': 200, 'ledger': 'ledger-op1'}
    def reconcile(route, identifier):
        if current['http'] != 200:
            fulfill(route, {'error': {'code': 'DENIED', 'message': 'Synthetic denied read'}}, current['http'])
        else:
            extra = {'ledger_sha256': current['ledger']}
            if current['status'] == 'mismatch':
                extra.update(observed={'quantity': '21', 'docstatus': 0},
                             differences=[{'field': 'quantity', 'expected': '20', 'observed': '21', 'reason': 'VALUE_MISMATCH'}])
            fulfill(route, receipt(identifier, current['status'], **extra))
    calls = mock_workspace(page, reconcile)
    page.get_by_test_id('open-recovery-detail').first.click()
    for status, label in [('missing', '查无记录不能证明未提交'), ('mismatch', '远端记录与原快照不一致'),
                          ('blocked', '本次核对被阻止'), ('unavailable', '本次无法完成核对')]:
        current['status'] = status
        page.get_by_test_id('reconcile-recovery').click()
        expect(page.get_by_test_id('recovery-result-status')).to_contain_text(label)
        if status == 'mismatch':
            expect(page.get_by_test_id('recovery-differences')).to_contain_text('预期 20；观测 21')
    current['ledger'] = 'different-ledger'
    page.get_by_test_id('reconcile-recovery').click()
    expect(page.get_by_test_id('recovery-reconcile-error')).to_contain_text('账本不一致')
    expect(page.get_by_test_id('recovery-reconciliation-result')).to_have_count(0)
    current['http'] = 403
    page.get_by_test_id('reconcile-recovery').click()
    expect(page.get_by_test_id('recovery-reconcile-error')).to_contain_text('DENIED')
    expect(page.get_by_test_id('identity')).to_have_text('buyer')
    current['http'] = 401
    page.get_by_test_id('reconcile-recovery').click()
    expect(page.get_by_test_id('recovery-panel')).to_have_count(0)
    expect(page.get_by_test_id('login-demo-buyer')).to_be_visible()
    assert all(method == 'GET' for method, _ in calls)


def test_native_recovery_multi_item_values_and_differences_stay_read_only(page):
    """Display-only synthetic multi-line receipt; no live ERP behavior is claimed."""
    expected = {'lines': [{'sku': 'A', 'quantity': '2', 'unit_price': '10.00', 'uom': 'EA'},
                          {'sku': 'B', 'quantity': '3', 'unit_price': '20.00', 'uom': 'EA'}], 'docstatus': 0}
    observed = {'lines': [{**expected['lines'][0], 'quantity': '4'}, expected['lines'][1]], 'docstatus': 0}
    def read_detail(route, identifier):
        fulfill(route, {**detail(identifier), 'expected': expected})
    def reconcile(route, identifier):
        fulfill(route, receipt(identifier, 'mismatch', expected=expected, observed=observed,
            differences=[{'field': 'lines[0].quantity', 'expected': '2', 'observed': '4', 'reason': 'VALUE_MISMATCH'}]))
    calls = mock_workspace(page, reconcile, detail_handler=read_detail)
    page.get_by_test_id('open-recovery-detail').first.click()
    page.get_by_test_id('recovery-detail').get_by_text('查看原始预期字段', exact=True).click()
    expect(page.get_by_test_id('recovery-line-values')).to_contain_text('A')
    expect(page.get_by_test_id('recovery-line-values')).to_contain_text('20.00')
    page.get_by_test_id('reconcile-recovery').click()
    expect(page.get_by_test_id('recovery-result-status')).to_contain_text('远端记录与原快照不一致')
    expect(page.get_by_test_id('recovery-differences')).to_contain_text('lines[0].quantity：预期 2；观测 4')
    expect(page.get_by_test_id('recovery-panel')).not_to_contain_text('[object Object]')
    expect(page.get_by_test_id('recovery-panel')).to_contain_text('禁止重放')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert calls and all(method == 'GET' for method, _ in calls)
