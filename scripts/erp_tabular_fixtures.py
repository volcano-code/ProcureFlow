"""Deterministic, ordinary-row CSV/XLSX input for the synthetic ERP lab.

These are real file bytes, not parser output. The selected quotation is behind a
cover/header row, a decoy SKU, reordered Chinese headers and an identical row.
The XLSX has a separate cover worksheet; CSV includes a multiline decoy note.
No parser, adapter or cost calculator is used to generate expected values.
"""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

from cost_fixtures import ACCEPTANCE_CASES

COLUMN_MAPPING = {'currency': 'B', 'shipping_cost': 'C', 'supplier_id': 'D',
    'tax_mode': 'E', 'sku': 'F', 'discount': 'G', 'unit_price': 'H',
    'delivery_days': 'I', 'tax_rate': 'J', 'quantity': 'K', 'uom': 'L'}
HEADERS = ['备注', '币种', '运费', '供应商编码', '税价模式', '物料编码', '折扣金额',
    '单价', '交期天数', '税率', '数量', '单位']
HEADER_ROW, SELECTED_ROW = 3, 5
SHEETS = {'csv': 'CSV', 'xlsx': '报价明细'}


def expected_values(data, scenario):
    case = ACCEPTANCE_CASES[scenario]
    return {'supplier_id': data['supplier'], 'sku': data['sku'], 'quantity': '20',
        'uom': 'EA', 'delivery_days': 7, 'currency': 'CNY', **{key: case[key] for key in
            ('unit_price', 'tax_mode', 'tax_rate', 'shipping_cost', 'discount')}}


def table_rows(data, scenario):
    values = expected_values(data, scenario)
    raw = {**values, 'currency': '人民币', 'tax_rate': '13%',
           'tax_mode': '含税' if values['tax_mode'] == 'included' else '未税'}
    selected = ['明确选择此报价'] + [str(raw[key]) for key in COLUMN_MAPPING]
    decoy = selected.copy()
    decoy[0], decoy[5], decoy[7] = '另一商品\n不可合并采购', 'PF-DECOY-ITEM', '1.00'
    return [['ProcureFlow 合成报价验收，仅供测试'], ['请显式选择工作表、表头、报价行和列'],
            HEADERS, decoy, selected, selected.copy()]


def _xlsx(sheets):
    output = io.BytesIO()
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relationships = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(1, len(sheets) + 1)) + '</Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{relationships}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{namespace}" xmlns:r="{relationships}"><sheets>'
            + ''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + ''.join(f'<Relationship Id="rId{i}" Type="{relationships}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(sheets) + 1)) + '</Relationships>',
    }
    for index, (_, rows) in enumerate(sheets, 1):
        files[f'xl/worksheets/sheet{index}.xml'] = f'<worksheet xmlns="{namespace}"><sheetData>' + ''.join(
            f'<row r="{row}">' + ''.join(
                f'<c r="{chr(64 + column)}{row}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
                for column, value in enumerate(values, 1)) + '</row>'
            for row, values in enumerate(rows, 1)) + '</sheetData></worksheet>'
    with zipfile.ZipFile(output, 'w') as archive:
        for name, value in files.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value.encode('utf-8'))
    return output.getvalue()


def source_file(data, scenario):
    kind = ACCEPTANCE_CASES[scenario]['input_format']
    rows = table_rows(data, scenario)
    if kind == 'csv':
        output = io.StringIO(newline='')
        csv.writer(output).writerows(rows)
        content = output.getvalue().encode('utf-8-sig')
    elif kind == 'xlsx':
        content = _xlsx([('说明', [['仅供测试，真正报价位于第二张工作表'], ['不要从封面导入']]),
                         (SHEETS[kind], rows)])
    else:
        raise ValueError('TABULAR_FIXTURE_REQUIRED')
    return f'synthetic-{scenario}.{kind}', content



def save_source_files(directory, data):
    """Retain only synthetic quotation bytes for independent artifact hashing."""
    destination = Path(directory) / 'input-fixtures'
    destination.mkdir(parents=True, exist_ok=True)
    for scenario, case in ACCEPTANCE_CASES.items():
        if case['input_format'] != 'txt':
            filename, content = source_file(data, scenario)
            (destination / filename).write_bytes(content)


def assert_provenance(data, scenario, content, mapped, quote, document):
    """Compare actual production API evidence to independent fixture coordinates."""
    kind = ACCEPTANCE_CASES[scenario]['input_format']
    sha = hashlib.sha256(content).hexdigest()
    wanted = expected_values(data, scenario)
    assert mapped['values'] == quote['values'] == wanted
    assert quote['document_sha256'] == mapped['document_sha256'] == document['sha256'] == sha
    assert quote['document_id'] == document['id'] == 'doc_' + mapped['id'][4:]
    assert set(quote['evidence']) == set(COLUMN_MAPPING)
    assert len(document['fragments']) == len(COLUMN_MAPPING)
    assert mapped['evidence'] == quote['evidence']
    fragments = {fragment['id']: fragment for fragment in document['fragments']}
    raw = table_rows(data, scenario)[SELECTED_ROW - 1]
    for field, column in COLUMN_MAPPING.items():
        evidence = quote['evidence'][field]
        locator = {'kind': kind, 'sheet': SHEETS[kind], 'row': SELECTED_ROW, 'column': column,
            'cell_range': column + str(SELECTED_ROW), 'header_row': HEADER_ROW,
            'label_cell': column + str(HEADER_ROW)}
        if kind == 'csv':
            locator.update(line_start=6, line_end=6)
        position = ord(column) - ord('A')
        text = f'{HEADERS[position]}: {raw[position]}'
        assert evidence == {'document_id': document['id'], 'document_sha256': sha,
            'fragment_id': evidence['fragment_id'], 'text': text, **locator}
        fragment = fragments[evidence['fragment_id']]
        assert fragment == {'id': evidence['fragment_id'], 'text': text, 'locator': locator}
    return {'document_sha256': sha, 'document_id': document['id'], 'import_id': mapped['id'],
        'quote_id': quote['id'], 'selected_sheet': SHEETS[kind], 'header_row': HEADER_ROW,
        'selected_row': SELECTED_ROW, 'column_mapping': dict(COLUMN_MAPPING),
        'quote_values': quote['values'], 'field_evidence': quote['evidence']}


def import_tabular_quote(call, request_id, data, scenario, restart):
    """Real HTTP workflow shared by the isolated lab and its offline harness test."""
    kind = ACCEPTANCE_CASES[scenario]['input_format']
    filename, content = source_file(data, scenario)
    path = f'/requests/{request_id}/table-imports'
    upload = lambda: call('POST', path, expected=201, files={'file': (filename, content)})
    uploaded = upload()
    assert uploaded['document_sha256'] == hashlib.sha256(content).hexdigest()
    assert uploaded['selection'] is None and uploaded['values'] is None and not uploaded['can_confirm']
    assert uploaded['quote_id'] is None and uploaded['status'] == 'OPEN'
    assert upload()['id'] == uploaded['id']
    assert call('GET', f'/requests/{request_id}/quotes') == []
    root = '/table-imports/' + uploaded['id']
    call('GET', root, role='other', expected=404)
    call('POST', root + '/confirm', expected=409,
         json={'expected_revision': uploaded['revision'], 'acknowledge': True})
    mapping = {'expected_revision': uploaded['revision'], 'sheet': SHEETS[kind],
        'header_row': HEADER_ROW, 'row': SELECTED_ROW, 'mapping': dict(COLUMN_MAPPING)}
    call('POST', root + '/preview', expected=422, json={**mapping, 'row': 4})
    mapped = call('POST', root + '/preview', json=mapping)
    assert mapped['selection'] == {key: value for key, value in mapping.items() if key != 'expected_revision'}
    assert mapped['values'] == expected_values(data, scenario) and mapped['can_confirm']
    assert call('GET', f'/requests/{request_id}/quotes') == []
    assert any('DUPLICATE_ROW:6:5' in issue for issue in mapped['issues'])
    restart()
    assert call('GET', root) == mapped
    assert call('GET', f'/requests/{request_id}/quotes') == []
    call('POST', root + '/confirm', expected=409,
         json={'expected_revision': uploaded['revision'], 'acknowledge': True})
    command = {'expected_revision': mapped['revision'], 'acknowledge': True}
    quote = call('POST', root + '/confirm', json=command)
    assert quote['version'] == 1 and quote['confirmed_by'] is None and not quote['calculation']['eligible']
    assert call('POST', f'/requests/{request_id}/analyze', json={})['proposal'] is None
    assert call('POST', root + '/confirm', json=command) == quote
    restart()
    assert call('POST', root + '/confirm', json=command) == quote
    duplicate = upload()
    assert duplicate['id'] == uploaded['id'] and duplicate['quote_id'] == quote['id']
    assert duplicate['status'] == 'IMPORTED'
    quotes = call('GET', f'/requests/{request_id}/quotes')
    assert len(quotes) == 1 and quotes[0] == quote
    document = call('GET', f"/documents/{quote['document_id']}/evidence")
    provenance = assert_provenance(data, scenario, content, mapped, quote, document)
    return quote, {'input_format': kind, 'tabular_provenance_verified': True,
        'duplicate_import_reused': True, 'preview_import_restart_verified': True,
        'provenance': provenance}
