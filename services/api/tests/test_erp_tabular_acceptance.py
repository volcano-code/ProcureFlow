"""Offline acceptance-harness regression. Fake HTTP ERP is NEVER live ERP proof."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'integrations/erpnext/sandbox'))
import erp_tabular_fixtures as fixtures
from cost_fixtures import ACCEPTANCE_CASES, COST_CASES
from check_erp_sandbox_evidence import require_tabular_provenance
from procureflow.tabular import parse_table, map_table

DATA = {'supplier': 'PF Synthetic Supplier', 'sku': 'PF-SANDBOX-ITEM',
    'company': 'ProcureFlow Sandbox', 'user': 'pf-integration@example.invalid',
    'api_key': 'synthetic-local-only', 'api_secret': 'synthetic-local-only'}
SCENARIOS = [key for key, case in ACCEPTANCE_CASES.items() if case['input_format'] != 'txt']


def parsed_fixture(scenario):
    filename, content = fixtures.source_file(DATA, scenario)
    sha = hashlib.sha256(content).hexdigest()
    kind = ACCEPTANCE_CASES[scenario]['input_format']
    table = parse_table(filename, content)
    parsed = map_table(table, fixtures.SHEETS[kind], fixtures.HEADER_ROW, fixtures.SELECTED_ROW,
        fixtures.COLUMN_MAPPING, 'doc_' + 'a' * 32, sha)
    mapped = {**parsed, 'id': 'tim_' + 'a' * 32, 'document_sha256': sha}
    quote = {**parsed, 'id': 'quo_' + 'b' * 32, 'document_id': 'doc_' + 'a' * 32, 'document_sha256': sha}
    document = {'id': quote['document_id'], 'sha256': sha, 'fragments': parsed['fragments']}
    return content, table, mapped, quote, document


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_real_csv_xlsx_bytes_select_only_explicit_quote_row_and_exact_evidence(scenario):
    content, table, mapped, quote, document = parsed_fixture(scenario)
    assert content == fixtures.source_file(DATA, scenario)[1]
    assert table['kind'] == ACCEPTANCE_CASES[scenario]['input_format']
    assert table['sheets'][-1]['rows'][3]['cells'][5]['value'] == 'PF-DECOY-ITEM'
    if table['kind'] == 'xlsx':
        assert len(table['sheets']) == 2 and table['sheets'][0]['name'] == '说明'
    assert any('DUPLICATE_ROW:6:5' in issue for issue in mapped['issues'])
    result = fixtures.assert_provenance(DATA, scenario, content, mapped, quote, document)
    require_tabular_provenance({'scenario': scenario, 'provenance': result})
    assert result['quote_values'] == fixtures.expected_values(DATA, scenario)
    assert result['selected_row'] == 5 and result['header_row'] == 3


@pytest.mark.parametrize('mutation', ['source', 'row', 'column', 'sheet', 'header', 'label',
    'cell', 'hash', 'document', 'fragment', 'text', 'value', 'physical-line', 'missing-field'])
def test_exact_provenance_refuses_drift(mutation):
    scenario = 'csv-excluded-discount'
    content, _, mapped, quote, document = parsed_fixture(scenario)
    # Break both the returned quote and mapped evidence to avoid relying only on
    # agreement between two views of the same database row.
    evidence = quote['evidence']['shipping_cost']
    if mutation == 'source': content += b'changed'
    elif mutation == 'row': evidence['row'] = 6
    elif mutation == 'column': evidence['column'] = 'D'
    elif mutation == 'sheet': evidence['sheet'] = 'other'
    elif mutation == 'header': evidence['header_row'] = 1
    elif mutation == 'label': evidence['label_cell'] = 'C1'
    elif mutation == 'cell': evidence['cell_range'] = 'C6'
    elif mutation == 'hash': evidence['document_sha256'] = 'f' * 64
    elif mutation == 'document': evidence['document_id'] = 'doc_other'
    elif mutation == 'fragment': evidence['fragment_id'] = 'missing'
    elif mutation == 'text': evidence['text'] = '运费: 0.00'
    elif mutation == 'value': quote['values']['shipping_cost'] = '0.00'
    elif mutation == 'physical-line': evidence['line_start'] = 5
    elif mutation == 'missing-field': del quote['evidence']['shipping_cost']
    with pytest.raises((AssertionError, KeyError)):
        fixtures.assert_provenance(DATA, scenario, content, mapped, quote, document)



def test_saved_tabular_source_files_are_exact_reproducible_inputs_without_credentials(tmp_path):
    fixtures.save_source_files(tmp_path, DATA)
    assert sorted(path.name for path in (tmp_path / 'input-fixtures').iterdir()) == sorted(
        fixtures.source_file(DATA, scenario)[0] for scenario in SCENARIOS)
    for scenario in SCENARIOS:
        filename, content = fixtures.source_file(DATA, scenario)
        assert (tmp_path / 'input-fixtures' / filename).read_bytes() == content
        assert DATA['api_secret'].encode() not in content


def persisted_cost(body, index):
    """Independent fixed cost expectations. Not ERPNext tax-engine execution."""
    cases = [case for case in COST_CASES.values() if Decimal(case['unit_price']) == Decimal(str(body['items'][0]['rate']))]
    case, = cases
    record = deepcopy(body)
    record.update(name=f'OFFLINE-SQ-{index}', total=case['goods'], net_total=case['net'],
        grand_total=case['total'], total_taxes_and_charges=str(Decimal(case['tax_after']) + Decimal(case['shipping_cost'])))
    record['items'][0].update(amount=case['goods'], net_amount=case['net'],
        net_rate=str((Decimal(case['net']) / 20).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')))
    tax, freight = record['taxes']
    tax.update(tax_amount=case['tax_before'], tax_amount_after_discount_amount=case['tax_after'],
        total=str(Decimal(case['net']) + Decimal(case['tax_after'])))
    freight.update(rate=None, tax_amount_after_discount_amount=case['shipping_cost'], total=case['total'])
    return record


@contextmanager
def offline_erp_server():
    state = {'documents': {}, 'posts': [], 'unexpected': []}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            url = urlsplit(self.path)
            path, query = unquote(url.path), parse_qs(url.query)
            if path == '/api/method/frappe.auth.get_logged_user':
                self.reply({'message': DATA['user']}); return
            if path == '/api/resource/Company/' + DATA['company']:
                value = {'name': DATA['company'], 'default_currency': 'CNY'}
            elif path == '/api/resource/Supplier':
                value = [{'name': DATA['supplier']}]
            elif path == '/api/resource/Custom Field':
                filters = json.loads(query['filters'][0])
                field = filters[-1][-1]
                value = [{'fieldname': field, 'unique': 1, 'fieldtype': 'Data'}]
            elif path == '/api/resource/Supplier Quotation':
                filters = json.loads(query['filters'][0])
                key = filters[-1][-1] if isinstance(filters, list) else filters['custom_procureflow_operation_key']
                value = [{'name': doc['name']} for doc in state['documents'].values()
                         if doc['custom_procureflow_operation_key'] == key]
            elif path.startswith('/api/resource/Supplier Quotation/'):
                value = state['documents'].get(path.rsplit('/', 1)[1])
            else:
                state['unexpected'].append(('GET', path)); self.send_error(404); return
            self.reply({'data': value})
        def do_POST(self):
            if unquote(urlsplit(self.path).path) != '/api/resource/Supplier Quotation':
                state['unexpected'].append(('POST', self.path)); self.send_error(403); return
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['posts'].append(body)
            doc = persisted_cost(body, len(state['posts']))
            state['documents'][doc['name']] = doc
            self.reply({'data': doc})
        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)



def test_fixture_export_failure_stops_before_erp_network(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('pf_tabular_export_failure', ROOT / 'scripts/verify_erp_sandbox.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'validate_lab', lambda *_: DATA)
    def fail_export(*_):
        raise OSError('PRIVATE_EXPORT_ERROR')
    monkeypatch.setattr(runner, 'save_source_files', fail_export)
    monkeypatch.setattr(runner, 'exercise', lambda *_: pytest.fail('must not network'))
    output = tmp_path / 'roundtrip.json'
    assert runner.main(['--ephemeral-test', '--output', str(output)]) == 1
    result = json.loads(output.read_text())
    assert result['network_attempted'] is False and result['status'] == 'failed'
    assert 'PRIVATE_EXPORT_ERROR' not in output.read_text()


def test_complete_http_worker_harness_with_offline_erp_double(monkeypatch):
    # This runs real API + fresh independent Worker processes and API restarts,
    # but the ERP server is a test double. Its output is not published as live evidence.
    spec = importlib.util.spec_from_file_location('pf_tabular_harness', ROOT / 'scripts/verify_erp_sandbox.py')
    runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    def raise_test_failure(error):
        raise error
    monkeypatch.setattr(runner, 'safe_failure', raise_test_failure)
    with offline_erp_server() as (url, state):
        monkeypatch.setattr(runner, 'TARGET', url)
        result = runner.exercise(DATA)
    assert result['status'] == 'passed', result
    assert result['post_attempts'] == len(ACCEPTANCE_CASES) == len(state['posts']) == 8
    assert result['real_erpnext_drafts_verified'] == 8  # harness field, fake upstream explicitly scoped above
    assert not state['unexpected']
    assert len(state['documents']) == 8
    assert [operation['scenario'] for operation in result['operations']] == list(ACCEPTANCE_CASES)
    assert all(document['docstatus'] == 0 for document in state['documents'].values())
    assert len({body['custom_procureflow_operation_key'] for body in state['posts']}) == 8
    assert result['steps'][-1] == 'api_process_restart_retains_remote_ids'
    for op in result['operations'][4:]:
        require_tabular_provenance(op)
        assert op['tabular_provenance_verified'] and op['duplicate_import_reused'] and op['preview_import_restart_verified']
