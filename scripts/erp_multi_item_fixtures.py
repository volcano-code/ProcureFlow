"""Independent two-row CSV input and provenance expectations for disposable ERP CI."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re

from cost_fixtures import MULTI_ITEM_CASE, MULTI_ITEM_SCENARIO

FIELDS = ('supplier_id', 'currency', 'shipping_cost', 'sku', 'quantity', 'uom',
          'unit_price', 'tax_mode', 'tax_rate', 'discount', 'delivery_days')
MAPPING = {field: chr(65 + i) for i, field in enumerate(FIELDS)}
LINE_FIELDS = ('sku', 'quantity', 'uom', 'unit_price', 'tax_mode', 'tax_rate', 'discount', 'delivery_days')
HEADER_FIELDS = ('supplier_id', 'currency', 'shipping_cost')


def expected_values(data):
    return {'supplier_id': data['supplier'], 'currency': 'CNY', 'shipping_cost': '5.25',
        'sku': None, 'quantity': None, 'uom': None, 'unit_price': None, 'tax_mode': 'unknown',
        'tax_rate': None, 'discount': None, 'delivery_days': None,
        'lines': [{field: line[field] for field in LINE_FIELDS} for line in MULTI_ITEM_CASE['lines']]}


def source_file(data):
    values = expected_values(data)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(FIELDS)
    for line in values['lines']:
        writer.writerow([{**values, **line}[field] for field in FIELDS])
    return f'synthetic-{MULTI_ITEM_SCENARIO}.csv', stream.getvalue().encode('utf-8')


def save_source_file(directory, data):
    filename, source = source_file(data)
    target = Path(directory) / 'input-fixtures'
    target.mkdir(parents=True, exist_ok=True)
    (target / filename).write_bytes(source)


def expected_evidence(data, document_id, sha):
    values = expected_values(data)
    sheet_id = hashlib.sha256(b'CSV').hexdigest()[:12]
    def cell(field, index):
        row, column = index + 2, MAPPING[field]
        value = {**values, **values['lines'][index]}[field]
        return {'kind': 'csv', 'document_id': document_id, 'document_sha256': sha,
            'fragment_id': f'{document_id}:s{sheet_id}:r{row:05d}:{column}',
            'text': f'{field}: {value}', 'sheet': 'CSV', 'row': row, 'column': column,
            'cell_range': f'{column}{row}', 'header_row': 1, 'label_cell': f'{column}1',
            'line_start': row, 'line_end': row}
    fields = {f'lines.{index}.{field}': cell(field, index) for index in range(2) for field in LINE_FIELDS}
    fields.update({field: {**cell(field, 0), 'sources': [cell(field, 0), cell(field, 1)]}
                   for field in HEADER_FIELDS})
    return fields


def require_multi_item_provenance(operation):
    """Exact two-row/source/values contract, independent of application parsers."""
    def require(condition):
        if not condition:
            raise ValueError('INVALID_MULTI_ITEM_PROVENANCE')
    p = operation.get('provenance')
    require(isinstance(p, dict))
    for field, prefix in (('import_id', 'tim_'), ('quote_id', 'quo_')):
        require(isinstance(p.get(field), str) and re.fullmatch(prefix + '[0-9a-f]{32}', p[field]) is not None)
    document_id = 'doc_' + p['import_id'][4:]
    values = p.get('quote_values')
    require(isinstance(values, dict))
    supplier = values.get('supplier_id')
    require(isinstance(supplier, str) and 0 < len(supplier) <= 80 and supplier.strip() == supplier)
    data = {'supplier': supplier}
    sha = hashlib.sha256(source_file(data)[1]).hexdigest()
    expected = {'document_sha256': sha, 'document_id': document_id,
        'import_id': p['import_id'], 'quote_id': p['quote_id'],
        'selected_sheet': 'CSV', 'header_row': 1, 'selected_rows': [2, 3],
        'column_mapping': MAPPING, 'quote_values': expected_values(data),
        'field_evidence': expected_evidence(data, document_id, sha)}
    # JSON equality is type-sensitive for boolean/integer controls and permits
    # object-key reordering while requiring every row and repeated header cell.
    require(json.dumps(p, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False))


def import_multi_item_quote(call, request_id, data, restart):
    filename, content = source_file(data)
    path = f'/requests/{request_id}/table-imports'
    upload = lambda: call('POST', path, expected=201, files={'file': (filename, content)})
    uploaded = upload()
    assert upload()['id'] == uploaded['id']
    root = '/table-imports/' + uploaded['id']
    command = {'expected_revision': uploaded['revision'], 'sheet': 'CSV', 'header_row': 1,
               'rows': [2, 3], 'mapping': MAPPING}
    mapped = call('POST', root + '/preview', json=command)
    assert mapped['can_confirm'] and mapped['values'] == expected_values(data)
    restart()
    assert call('GET', root) == mapped
    confirm = {'expected_revision': mapped['revision'], 'acknowledge': True}
    quote = call('POST', root + '/confirm', json=confirm)
    assert quote['version'] == 1 and quote['confirmed_by'] is None
    assert call('POST', root + '/confirm', json=confirm) == quote
    assert upload()['quote_id'] == quote['id']
    assert quote['values'] == expected_values(data)
    document = call('GET', f"/documents/{quote['document_id']}/evidence")
    sha = hashlib.sha256(content).hexdigest()
    assert quote['document_sha256'] == document['sha256'] == sha
    assert len(document['fragments']) == 22 and len({f['id'] for f in document['fragments']}) == 22
    provenance = {'document_sha256': sha, 'document_id': quote['document_id'],
        'import_id': uploaded['id'], 'quote_id': quote['id'], 'selected_sheet': 'CSV',
        'header_row': 1, 'selected_rows': [2, 3], 'column_mapping': MAPPING,
        'quote_values': quote['values'], 'field_evidence': quote['evidence']}
    require_multi_item_provenance({'provenance': provenance})
    return quote, {'input_format': 'csv', 'multi_item': True, 'multi_item_provenance_verified': True,
        'duplicate_import_reused': True, 'preview_import_restart_verified': True, 'provenance': provenance}
