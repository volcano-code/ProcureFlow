"""Check all disposable ERP gate records; not an independent execution or attestation."""
from __future__ import annotations
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrations/erpnext/sandbox'))
from probe_erp_permissions import require_probe_evidence, PROBES, REQUIRED_TRUE as PROBE_REQUIRED_TRUE
from lab_permissions import require_select_only
from cost_fixtures import ACCEPTANCE_CASES, acceptance_cases, MULTI_ITEM_SCENARIO
from erp_multi_item_fixtures import require_multi_item_provenance

FILES = ('seed.json', 'roundtrip.json', 'database-audit.json', 'image-digests.json', 'permission-probes.json')
MAX_SOURCE_BYTES = 2 * 1024 * 1024
STEPS = [
    'dedicated_identity_get_only_preflight',
    'unapproved_self_approved_and_stale_writes_denied',
    'tabular_preview_import_and_stale_quote_policy_writes_denied',
    *(f'{scenario}_independent_worker_draft_readback_and_replay' for scenario in ACCEPTANCE_CASES),
    'api_process_restart_retains_remote_ids',
]


TABULAR_MAPPING = {
    'currency': 'B', 'shipping_cost': 'C', 'supplier_id': 'D', 'tax_mode': 'E',
    'sku': 'F', 'discount': 'G', 'unit_price': 'H', 'delivery_days': 'I',
    'tax_rate': 'J', 'quantity': 'K', 'uom': 'L',
}

TABULAR_HEADERS = {
    'currency': '币种', 'shipping_cost': '运费', 'supplier_id': '供应商编码', 'tax_mode': '税价模式',
    'sku': '物料编码', 'discount': '折扣金额', 'unit_price': '单价', 'delivery_days': '交期天数',
    'tax_rate': '税率', 'quantity': '数量', 'uom': '单位',
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def count_is(value, expected: int) -> bool:
    return type(value) is int and value == expected


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def require_tabular_provenance(operation: dict) -> None:
    """Check source/selection/quote links, never infer provenance from a total."""
    fixture = ACCEPTANCE_CASES[operation['scenario']]
    kind = fixture['input_format']
    provenance = operation.get('provenance')
    require(isinstance(provenance, dict), 'TABULAR_PROVENANCE_MISSING')
    sha = provenance.get('document_sha256')
    require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha) is not None,
            'INVALID_TABULAR_SOURCE_HASH')
    for key, prefix in (('import_id', 'tim_'), ('quote_id', 'quo_')):
        value = provenance.get(key)
        require(isinstance(value, str) and re.fullmatch(prefix + '[0-9a-f]{32}', value) is not None,
                'INVALID_TABULAR_ID')
    document_id = 'doc_' + provenance['import_id'][4:]
    require(provenance.get('document_id') == document_id, 'INVALID_TABULAR_DOCUMENT_ID')
    sheet = 'CSV' if kind == 'csv' else '报价明细'
    require(provenance.get('selected_sheet') == sheet
            and count_is(provenance.get('header_row'), 3)
            and count_is(provenance.get('selected_row'), 5)
            and provenance.get('column_mapping') == TABULAR_MAPPING, 'INVALID_TABULAR_SELECTION')
    values = provenance.get('quote_values')
    require(isinstance(values, dict) and set(values) == set(TABULAR_MAPPING), 'INVALID_TABULAR_VALUES')
    supplier = values.get('supplier_id')
    require(isinstance(supplier, str) and 0 < len(supplier) <= 80 and supplier.strip() == supplier,
            'INVALID_TABULAR_VALUES')
    expected = {key: fixture[key] for key in ('unit_price', 'tax_mode', 'tax_rate', 'shipping_cost', 'discount')}
    expected.update(supplier_id=supplier, sku='PF-SANDBOX-ITEM', quantity='20',
                    uom='EA', currency='CNY', delivery_days=7)
    require(values == expected and count_is(values.get('delivery_days'), 7), 'INVALID_TABULAR_VALUES')
    fields = provenance.get('field_evidence')
    require(isinstance(fields, dict) and set(fields) == set(TABULAR_MAPPING), 'INVALID_TABULAR_FIELD_EVIDENCE')
    raw_values = {**expected, 'currency': '人民币', 'tax_rate': '13%',
                  'tax_mode': '含税' if fixture['tax_mode'] == 'included' else '未税'}
    # map_table assigns fragment IDs in submitted mapping order. JSON object
    # order is not part of the evidence contract: bind each field to its exact
    # source coordinates/text, and require the complete unique fragment set.
    expected_fragments = {f'{document_id}:f{index:04d}' for index in range(len(TABULAR_MAPPING))}
    fragment_ids = set()
    for field, column in TABULAR_MAPPING.items():
        evidence = fields[field]
        require(isinstance(evidence, dict), 'INVALID_TABULAR_FIELD_EVIDENCE')
        fragment_id = evidence.get('fragment_id')
        require(type(fragment_id) is str and fragment_id in expected_fragments
                and fragment_id not in fragment_ids, 'INVALID_TABULAR_FIELD_EVIDENCE')
        fragment_ids.add(fragment_id)
        locator = {'kind': kind, 'document_id': document_id, 'document_sha256': sha,
            'fragment_id': fragment_id, 'sheet': sheet, 'row': 5,
            'column': column, 'cell_range': f'{column}5', 'header_row': 3, 'label_cell': f'{column}3'}
        if kind == 'csv':
            locator.update(line_start=6, line_end=6)
        require(set(evidence) == set(locator) | {'text'}
                and all(evidence.get(key) == value and type(evidence.get(key)) is type(value)
                        for key, value in locator.items()), 'INVALID_TABULAR_FIELD_EVIDENCE')
        text = evidence.get('text')
        require(text == f'{TABULAR_HEADERS[field]}: {raw_values[field]}', 'INVALID_TABULAR_FIELD_TEXT')
    require(fragment_ids == expected_fragments, 'INVALID_TABULAR_FIELD_EVIDENCE')


def check(directory: Path, backend='sqlite', include_multi_item=False) -> dict:
    cases = acceptance_cases(include_multi_item)
    expected_steps = STEPS[:-1] + ([MULTI_ITEM_SCENARIO + '_independent_worker_draft_readback_and_replay'] if include_multi_item else []) + STEPS[-1:]
    records, digests, source_digests = {}, {}, {}
    for name in FILES:
        with (directory / name).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, 'OVERSIZED_EVIDENCE')
        records[name] = json.loads(raw, object_pairs_hook=unique_object)
        digests[name] = hashlib.sha256(raw).hexdigest()
    seed, run, audit, images, probes = (records[name] for name in FILES)
    require_probe_evidence(probes, include_multi_item=include_multi_item)
    require(run.get('include_multi_item', False) is include_multi_item, 'MULTI_ITEM_SELECTION_MISMATCH')
    require(backend in {'sqlite', 'postgresql'}, 'INVALID_EXPECTED_BACKEND')
    require(all(isinstance(x, dict) and x.get('status') == 'passed' for x in (seed, run, audit)), 'INCOMPLETE_STAGES')
    require(seed.get('stage') == 'seed' and seed.get('synthetic_only') is True, 'INVALID_SEED')
    require(seed.get('integration_user_type') == 'System User', 'INVALID_INTEGRATION_USER_TYPE')
    require(all(record.get('currency_precision') == '2' and record.get('float_precision') == '6' and record.get('rounding_method') == 'Commercial Rounding'
                for record in (seed, audit)), 'UNVERIFIED_COST_PRECISION')
    if include_multi_item:
        require(count_is(seed.get('synthetic_item_count'), 2) and seed.get('multi_skus') == ['PF-SANDBOX-ITEM','PF-SANDBOX-ITEM-2'], 'MULTI_ITEM_SEED_MISSING')
        require(audit.get('multi_item_components_independently_checked') is True
                and count_is(audit.get('multi_item_draft_count'), 1) and count_is(audit.get('multi_item_row_count'), 2), 'MULTI_ITEM_DATABASE_AUDIT_MISSING')
    permissions = seed.get('permissions', {})
    require(all(permissions.get(p) is True for p in ('read', 'create', 'write')) and
            all(permissions.get(p) is False for p in ('submit', 'cancel', 'delete')), 'UNSAFE_SEED_PERMISSIONS')
    require(run.get('synthetic_only') is True and all(run.get(k) is False for k in
        ('real_user_account_used', 'human_approval_measured', 'model_used')), 'INVALID_SCOPE')
    require(run.get('steps') == expected_steps, 'MISSING_OR_REPEATED_STEPS')
    require(count_is(run.get('post_attempts'), len(cases)) and count_is(run.get('real_erpnext_drafts_verified'), len(cases)), 'INVALID_WRITE_COUNTS')
    require(run.get('api_business_database') == {'sqlite': 'SQLite', 'postgresql': 'PostgreSQL'}[backend]
            and run.get('real_erp_database') == 'MariaDB', 'INVALID_DATABASE_SCOPE')
    if backend == 'postgresql':
        business = run.get('business_database_audit', {})
        require(business.get('status') == 'passed' and business.get('database') == 'PostgreSQL', 'BUSINESS_AUDIT_MISSING')
        require(all(business.get(key) is True for key in ('isolated_schema', 'migration_current',
            'operation_identity_matches', 'read_only_audit', 'after_api_restart')), 'BUSINESS_AUDIT_INCOMPLETE')
        require(all(count_is(business.get(key), len(cases)) for key in ('operation_count', 'completed_operation_count',
            'outbox_count', 'done_outbox_count', 'verified_receipt_count')), 'BUSINESS_AUDIT_INVALID_COUNTS')
        require(isinstance(business.get('server_version_num'), str)
            and re.fullmatch('[0-9]{5,6}', business['server_version_num']) is not None, 'POSTGRES_VERSION_MISSING')
    preflight = run.get('preflight', {})
    require(preflight.get('integration_identity_verified') is True and preflight.get('company_currency_verified') is True
            and preflight.get('write_probe_performed') is False,
            'IDENTITY_NOT_VERIFIED')
    operations = run.get('operations')
    require(isinstance(operations, list) and len(operations) == len(cases) and all(isinstance(x, dict) for x in operations),
            'INVALID_OPERATIONS')
    require([x.get('scenario') for x in operations] == list(cases), 'INVALID_SCENARIOS')
    for op in operations:
        require(isinstance(op.get('operation_id'), str) and bool(op['operation_id']), 'INVALID_OPERATION_ID')
        require(isinstance(op.get('remote_id'), str) and bool(op['remote_id']) and not op['remote_id'].startswith('MOCK-'),
                'INVALID_REMOTE_ID')
        require(isinstance(op.get('snapshot_hash'), str) and re.fullmatch('[0-9a-f]{64}', op['snapshot_hash']) is not None,
                'INVALID_SNAPSHOT_HASH')
        fixture = cases[op['scenario']]
        require(op.get('expected_total') == fixture['total'] and op.get('cost_components_verified') is True, 'INVALID_EXPECTED_TOTAL')
        require(op.get('input_format') == fixture['input_format'], 'INVALID_INPUT_FORMAT')
        if fixture.get('multi_item'):
            require(op.get('multi_item') is True and count_is(op.get('item_count'), 2)
                    and count_is(op.get('shipping_count'), 1) and all(op.get(key) is True for key in
                    ('multi_item_provenance_verified', 'duplicate_import_reused', 'preview_import_restart_verified',
                     'independent_readback_verified', 'lost_receipt_reconciled', 'idempotent_replay_verified',
                     'single_post_verified', 'source_rows_verified')), 'MULTI_ITEM_CHECKS_MISSING')
            require_multi_item_provenance(op)
            require(op.get('contract_version') == 'multi-sku-v1'
                    and op.get('erp_cost_mapping_version') == 'multi-line-zero-tax-costs-v1'
                    and op.get('supplier_id') == op['provenance']['quote_values']['supplier_id']
                    and isinstance(op.get('transaction_date'), str)
                    and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', op['transaction_date']) is not None,
                    'MULTI_ITEM_SNAPSHOT_BINDING_MISSING')
            date.fromisoformat(op['transaction_date'])
        elif fixture['input_format'] in {'csv', 'xlsx'}:
            require(all(op.get(key) is True for key in ('tabular_provenance_verified',
                'duplicate_import_reused', 'preview_import_restart_verified')), 'TABULAR_CHECKS_MISSING')
            require_tabular_provenance(op)
    tabular = [op['provenance'] for op in operations if op['input_format'] in {'csv', 'xlsx'}]
    require(all(len({entry[key] for entry in tabular}) == len(tabular)
                for key in ('document_sha256', 'document_id', 'import_id', 'quote_id')), 'DUPLICATE_TABULAR_PROVENANCE')
    # The CI artifact retains the original upload bytes. Recompute their hashes
    # without parsing them or treating matching reports as a new ERP execution.
    for operation in operations:
        fixture = cases[operation['scenario']]
        if fixture['input_format'] == 'txt':
            continue
        filename = f"input-fixtures/synthetic-{operation['scenario']}.{fixture['input_format']}"
        with (directory / filename).open('rb') as stream:
            source = stream.read(MAX_SOURCE_BYTES + 1)
        require(len(source) <= MAX_SOURCE_BYTES, 'OVERSIZED_SOURCE_FIXTURE')
        require(bool(source), 'EMPTY_SOURCE_FIXTURE')
        source_digests[filename] = hashlib.sha256(source).hexdigest()
        require(source_digests[filename] == operation['provenance']['document_sha256'],
                'SOURCE_FIXTURE_HASH_MISMATCH')
    require(len({x['operation_id'] for x in operations}) == len(cases) and len({x['remote_id'] for x in operations}) == len(cases),
            'DUPLICATE_OPERATION_OR_DRAFT')
    require(audit.get('stage') == 'database-audit' and count_is(audit.get('draft_count'), len(cases)) and
            count_is(audit.get('purchase_order_count'), 0) and count_is(audit.get('submitted_count'), 0), 'INVALID_DATABASE_COUNTS')
    require_select_only(seed.get('reference_permissions'))
    require_select_only(audit.get('reference_permissions'))
    for record in (seed, audit):
        refs = record.get('cost_reference_permissions')
        require(isinstance(refs, dict) and set(refs) == {'tax', 'freight'}, 'COST_REFERENCE_PERMISSIONS_MISSING')
        for permissions in refs.values():
            require_select_only(permissions)
    require(all(audit.get(k) is True for k in ('remote_unique_index_present',
        'duplicate_key_update_rejected_and_rolled_back', 'adapter_readback_independently_checked', 'cost_components_independently_checked')), 'DATABASE_CHECKS_MISSING')
    require(all(audit.get('restricted_permissions', {}).get(k) is True for k in ('submit', 'cancel', 'delete')),
            'UNSAFE_AUDIT_PERMISSIONS')
    require(all(isinstance(audit.get(k), str) and bool(audit[k]) for k in
        ('erpnext_version', 'frappe_version', 'database_version')), 'VERSIONS_MISSING')
    require(isinstance(images, list) and len(images) > 0 and all(isinstance(x, str) and
        re.fullmatch(r'frappe/erpnext@sha256:[0-9a-f]{64}', x) for x in images), 'IMAGE_DIGEST_MISSING')
    return {'status': 'passed', 'record_sha256': digests, 'source_sha256': source_digests, 'synthetic_only': True,
        'normal_and_lost_receipt_checked': True, 'database_audit_checked': True,
        'tabular_import_evidence_checked': True, 'multi_item_evidence_checked': include_multi_item,
        'api_business_database': run['api_business_database'], 'negative_rest_permissions_checked': True,
        'business_database_audit_checked': backend == 'postgresql',
        'scope': 'Consistency check of CI records, not a new ERP execution or cryptographic attestation'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-multi-item', action='store_true')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--business-database', choices=('sqlite', 'postgresql'), default='sqlite')
    args = parser.parse_args(argv)
    try:
        result = check(args.directory, args.business_database, args.include_multi_item)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Never echo malformed record content or paths into public CI output.
        print(json.dumps({'status': 'failed', 'reason': 'ERP_EVIDENCE_INCOMPLETE_OR_INVALID'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
