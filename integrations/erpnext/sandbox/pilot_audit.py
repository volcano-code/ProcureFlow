"""Independent read-only MariaDB audit for the separate two-draft pilot lab.

The old eight-scenario audit remains unchanged. This script accepts only this
pilot's fixed two cases, three denied operations, and a fresh marked sandbox.
"""
import json
from pathlib import Path
from decimal import Decimal
import frappe
import erpnext
from seed import COMPANY, USER
from lab_runtime import lab_session, phase, run_stage
from lab_permissions import verify_account_reference
from cost_audit import verify_cost_document
from cost_fixtures import ACCEPTANCE_CASES, TAX_ACCOUNT, FREIGHT_ACCOUNT

SCENARIOS = ('csv-excluded-discount', 'xlsx-included-discount-lost-receipt')
DENIALS = ('buyer-revocation', 'approver-revocation', 'approver-membership-expiry')


def main():
    with lab_session():
        phase('pilot-database-evidence')
        report = json.loads(Path('/tmp/pf-pilot-result.json').read_text())
        assert report['status'] == 'passed' and report['fixture'] is False and report['live_erp'] is True
        operations, denials = report['operations'], report['denials']
        assert [op['scenario'] for op in operations] == list(SCENARIOS)
        assert [op['scenario'] for op in denials] == list(DENIALS)
        assert len({op['operation_id'] for op in operations + denials}) == 5
        assert frappe.db.get_value('Company', COMPANY, 'default_currency') == 'CNY'
        rows = frappe.get_all('Supplier Quotation', fields=['name', 'docstatus', 'grand_total',
            'custom_procureflow_operation_key', 'custom_procureflow_snapshot_hash'])
        assert len(rows) == 2 and frappe.db.count('Purchase Order') == 0
        for operation in operations:
            row, = [r for r in rows if r.custom_procureflow_operation_key == operation['operation_id']]
            assert row.name == operation['remote_id'] and row.docstatus == 0
            assert row.custom_procureflow_snapshot_hash == operation['snapshot_hash']
            assert Decimal(str(row.grand_total)) == Decimal(ACCEPTANCE_CASES[operation['scenario']]['total'])
            verify_cost_document(frappe.get_doc('Supplier Quotation', row.name).as_dict(), ACCEPTANCE_CASES[operation['scenario']])
        assert not {op['operation_id'] for op in denials} & {r.custom_procureflow_operation_key for r in rows}
        indexes = frappe.db.sql('SHOW INDEX FROM `tabSupplier Quotation`', as_dict=True)
        assert any(r.Column_name == 'custom_procureflow_operation_key' and r.Non_unique == 0 for r in indexes)
        denied = {p: not frappe.has_permission('Supplier Quotation', p, user=USER) for p in ('submit', 'cancel', 'delete')}
        assert all(denied.values()) and not frappe.has_permission('Purchase Order', 'create', user=USER)
        payable = frappe.db.get_value('Company', COMPANY, 'default_payable_account')
        refs = verify_account_reference(frappe.has_permission, USER, payable)
        cost_refs = {kind: verify_account_reference(frappe.has_permission, USER, account)
            for kind, account in [('tax', TAX_ACCOUNT), ('freight', FREIGHT_ACCOUNT)]}
        frappe.db.rollback()
        return {'status': 'passed', 'scope': 'pilot-native-two-draft', 'draft_count': 2,
            'purchase_order_count': 0, 'submitted_count': 0, 'denied_operation_draft_count': 0,
            'operation_count_checked': 5, 'read_only_audit': True, 'remote_unique_index_present': True,
            'adapter_readback_independently_checked': True, 'cost_components_independently_checked': True,
            'reference_permissions': refs, 'cost_reference_permissions': cost_refs,
            'restricted_permissions': denied, 'purchase_order_create_denied': True,
            'erpnext_version': erpnext.__version__, 'frappe_version': frappe.__version__,
            'database_version': frappe.db.sql('SELECT VERSION()')[0][0]}


if __name__ == '__main__': raise SystemExit(run_stage('database-audit', main))
