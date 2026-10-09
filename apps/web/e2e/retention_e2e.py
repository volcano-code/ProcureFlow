"""Strict local SQLite archive operator-to-native-UI gate; not a Compose substitute."""
import os
from pathlib import Path
from playwright.sync_api import expect
from workbench_e2e import page, prepare_table_import, upload_table, table_csv, URL


def test_native_table_archived_preview_readback_and_reopen_stay_read_only(page):
    """Real operator archive of this gate's synthetic SQLite preview, no mocked API."""
    from urllib.parse import urlparse
    from sqlalchemy.engine import make_url
    from procureflow.db import Database, TableImportRow
    from procureflow.retention import RetentionService

    api=os.environ.get('NEXT_PUBLIC_API_BASE_URL','').rstrip('/')
    if api.startswith('/'):
        api=URL.rstrip('/')+api
    database_url=os.environ.get('PF_DATABASE_URL','sqlite://')
    parsed=make_url(database_url)
    data_dir=Path(os.environ.get('PF_DATA_DIR','')).resolve()
    # Same gate-owned database boundary used by the pilot operator fixtures. Never
    # let a native UI invocation turn an arbitrary database into a mutation target.
    if (os.environ.get('PF_ALLOW_TEST_MUTATIONS')!='1' or os.environ.get('PF_MODE')!='demo'
            or os.environ.get('PF_ERP_MODE')!='mock'
            or urlparse(api).hostname not in {'127.0.0.1','localhost'}
            or parsed.get_backend_name()!='sqlite' or not data_dir.name.startswith('pf-next-e2e-')
            or Path(parsed.database or '').resolve()!=data_dir/'next.sqlite3'
            or not (data_dir/'next.sqlite3').is_file()):
        raise RuntimeError('Archive fixture requires the gate-owned temporary SQLite database and loopback mock API')
    health=page.request.get(api+'/health')
    assert health.status==200
    assert health.json()['mode']=='demo' and health.json()['erp']=='mock'

    prepare_table_import(page)
    with page.expect_response(lambda response: response.request.method=='POST'
                              and response.url.endswith('/table-imports')) as uploaded:
        upload_table(page,'archived-synthetic.csv',table_csv())
    assert uploaded.value.status==201
    draft=uploaded.value.json()
    page.get_by_test_id('preview-table-mapping').click()
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('1100.25')
    page.get_by_test_id('table-import-ack').check()
    expect(page.get_by_test_id('confirm-table-import')).to_be_enabled()
    confirms=[]
    page.on('request',lambda request:confirms.append(request.url) if request.method=='POST'
            and '/table-imports/' in request.url and request.url.endswith('/confirm') else None)

    db=Database(database_url)
    try:
        with db.transaction(write=True) as session:
            row=session.get(TableImportRow,draft['id'])
            assert row and row.tenant_id=='demo' and row.request_id==draft['request_id']
            assert row.status=='OPEN' and row.quote_id is None and row.parsed
            row.expires_at='2000-01-01T00:00:00+00:00'
        retention=RetentionService(db)
        plan=retention.plan('demo',import_ids=[draft['id']])
        result=retention.apply(plan,tenant_id='demo',actor='synthetic-browser-operator')
        assert result['changed']==[draft['id']] and result['files_deleted']==0
    finally:
        db.engine.dispose()

    page.get_by_test_id('read-table-import').click()
    expect(page.get_by_test_id('table-import-archived')).to_contain_text('原始文件和映射记录仍保留')
    expect(page.get_by_test_id('table-import-archived')).to_contain_text('取消归档不会恢复导入权限')
    expect(page.get_by_test_id('table-map-unit_price')).to_be_disabled()
    expect(page.get_by_test_id('preview-table-mapping')).to_be_disabled()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    expect(page.get_by_test_id('table-import-ack')).to_be_disabled()
    expect(page.get_by_test_id('table-import-ack')).not_to_be_checked()
    expect(page.get_by_test_id('table-preview-dirty')).to_have_count(0)
    expect(page.get_by_test_id('table-result-unit_price')).to_contain_text('1100.25')
    page.get_by_label('关闭表格导入').click()
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)

    # Opening a fresh dialog and re-uploading identical bytes must read back the
    # archived record, without reviving its mapping or replaying confirmation.
    with page.expect_response(lambda response: response.request.method=='POST'
                              and response.url.endswith('/table-imports')) as repeated:
        upload_table(page,'archived-synthetic.csv',table_csv())
    assert repeated.value.status==201
    saved=repeated.value.json()
    assert saved['id']==draft['id'] and saved['status']=='ARCHIVED' and saved['quote_id'] is None
    assert saved['can_confirm'] is False and saved['expires_at']=='2000-01-01T00:00:00+00:00'
    expect(page.get_by_test_id('table-import-archived')).to_be_visible()
    expect(page.get_by_test_id('table-map-unit_price')).to_be_disabled()
    expect(page.get_by_test_id('preview-table-mapping')).to_be_disabled()
    expect(page.get_by_test_id('confirm-table-import')).to_be_disabled()
    page.keyboard.press('Escape')
    expect(page.get_by_test_id('table-import-dialog')).to_have_count(0)
    expect(page.get_by_test_id('quote-row')).to_have_count(0)
    quotes=page.request.get(api+f"/api/v1/requests/{draft['request_id']}/quotes",
                            headers={'Authorization':'Bearer demo-buyer'})
    assert quotes.status==200 and quotes.json()==[]
    assert confirms==[]
