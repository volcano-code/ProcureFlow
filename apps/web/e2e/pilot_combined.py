"""Native pilot browser driver for the opt-in, disposable combined harness.

No route mocking, static bearer identity, page.evaluate business writes, or
same-page role switch. Credentials live only in this process's memory. Never
save traces/HTML/screenshots of invitation or session screens.
"""
from pathlib import Path
import sys
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'integrations/erpnext/sandbox'))
from erp_tabular_fixtures import COLUMN_MAPPING, SHEETS, source_file, assert_provenance
from cost_fixtures import ACCEPTANCE_CASES


def response_for(page, suffix, action, status=200):
    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith(suffix)) as pending:
        action()
    response = pending.value
    assert response.status == status, 'PILOT_BROWSER_RESPONSE_FAILED'
    return response.json()


def login(page, url, credential, role):
    page.goto(url)
    expect(page.get_by_test_id('login-demo-buyer')).to_have_count(0)
    page.get_by_test_id('login-credential').fill(credential)
    session = response_for(page, '/auth/login', lambda: page.get_by_test_id('login-submit').click())
    expect(page.get_by_test_id('identity')).to_have_text(role)
    expect(page.get_by_test_id('identity-tenant')).to_contain_text('lab')
    assert session['identity']['role'] == role and session['identity']['tenant_id'] == 'lab'
    # Authentication is intentionally in-memory, never local/session storage.
    assert page.evaluate('Object.keys(localStorage).length + Object.keys(sessionStorage).length') == 0
    return session


def prepare_case(browser, url, invitations, data, scenario, title):
    """Return live independent contexts plus private readback credentials."""
    buyer_context = browser.new_context(viewport={'width': 1440, 'height': 1100})
    approver_context = browser.new_context(viewport={'width': 1440, 'height': 1100})
    buyer, approver = buyer_context.new_page(), approver_context.new_page()
    business_requests, errors = [], []
    for page in (buyer, approver):
        page.on('pageerror', lambda _error: errors.append('PILOT_BROWSER_PAGE_ERROR'))
        page.on('request', lambda request: business_requests.append((request.method, request.url))
                if '/api/v1/' in request.url else None)
    try:
        buyer_session = login(buyer, url, invitations['buyer'], 'buyer')
        buyer.get_by_role('button', name='＋ 新建采购需求', exact=True).click()
        form = buyer.get_by_test_id('request-form')
        for field, value in {'title': title, 'sku': data['sku'], 'quantity': '20',
                             'budget': '30000.00', 'max_delivery_days': '14'}.items():
            form.locator(f'[name="{field}"]').fill(value)
        request = response_for(buyer, '/requests', lambda: form.get_by_role('button', name='保存需求').click(), 201)
        buyer.get_by_test_id('open-table-import').click()
        filename, content = source_file(data, scenario)
        buyer.get_by_test_id('table-import-file').set_input_files({'name': filename, 'mimeType':
            'text/csv' if filename.endswith('.csv') else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'buffer': content})
        uploaded = response_for(buyer, '/table-imports', lambda: buyer.get_by_test_id('upload-table-preview').click(), 201)
        kind = ACCEPTANCE_CASES[scenario]['input_format']
        buyer.get_by_test_id('table-sheet').select_option(SHEETS[kind])
        buyer.get_by_test_id('table-header-row').select_option('3')
        buyer.get_by_test_id('table-data-row').select_option('5')
        for field, column in COLUMN_MAPPING.items():
            buyer.get_by_test_id('table-map-' + field).select_option(column)
        mapped = response_for(buyer, '/preview', lambda: buyer.get_by_test_id('preview-table-mapping').click())
        expect(buyer.get_by_test_id('table-import-issues')).to_contain_text('DUPLICATE_ROW:6:5')
        buyer.get_by_test_id('table-import-ack').check()
        quote = response_for(buyer, '/confirm', lambda: buyer.get_by_test_id('confirm-table-import').click())
        assert quote['confirmed_by'] is None and quote['id'] != uploaded['id']
        buyer.get_by_test_id('finish-table-import').click()
        expect(buyer.get_by_test_id('quote-row')).to_have_count(1)
        buyer.get_by_test_id('confirm-quote').click()
        buyer.get_by_test_id('ack-confirm').check()
        confirmed = response_for(buyer, '/confirm', lambda: buyer.get_by_test_id('submit-confirm').click())
        assert confirmed['confirmed_by'] == buyer_session['identity']['user_id']
        comparison = response_for(buyer, '/analyze', lambda: buyer.get_by_test_id('analyze').click())
        proposal = comparison['proposal']
        assert comparison['llm_used'] is False and proposal['total'] == ACCEPTANCE_CASES[scenario]['total']
        approver_session = login(approver, url, invitations['approver'], 'approver')
        assert approver_session['identity']['user_id'] != buyer_session['identity']['user_id']
        # The approver's fresh context reads the request independently, without borrowing buyer state.
        approver.locator('#request-list').get_by_role('button').filter(has_text=title).click()
        expect(approver.get_by_test_id('proposal-body')).to_contain_text(proposal['total'])
        response_for(approver, '/approval', lambda: approver.get_by_test_id('approve').click())
        expect(approver.get_by_test_id('request-status')).to_have_text('已批准')
        buyer.get_by_role('button', name='刷新状态', exact=True).click()
        expect(buyer.get_by_test_id('execute')).to_be_enabled()
        operation = response_for(buyer, '/execute', lambda: buyer.get_by_test_id('execute').click(), 202)
        expect(buyer.get_by_test_id('execute')).to_have_count(0)
        assert operation['status'] == 'PENDING'
        assert not any(method == 'POST' and path.endswith('/process') for method, path in business_requests)
        assert all(path.startswith(url + '/backend/api/v1/') for _, path in business_requests)
        assert errors == []
        return {'buyer': buyer, 'approver': approver, 'contexts': [buyer_context, approver_context],
            'buyer_session': buyer_session, 'approver_session': approver_session, 'operation': operation,
            'proposal': proposal, 'quote': quote, 'mapped': mapped, 'content': content, 'request': request,
            'browser_checks': {'independent_contexts': True, 'distinct_users': True, 'native_invitation_login': True,
                'ordinary_table_import': True, 'explicit_quote_confirmation': True, 'independent_approval': True,
                'enqueue_only': True, 'same_origin_proxy': True, 'no_browser_credential_storage': True}}
    except Exception:
        buyer_context.close(); approver_context.close()
        raise


def logout(page):
    response_for(page, '/auth/logout', lambda: page.get_by_test_id('logout').click())
    expect(page.get_by_test_id('login-credential')).to_be_visible()
    expect(page.get_by_test_id('proposal-body')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
