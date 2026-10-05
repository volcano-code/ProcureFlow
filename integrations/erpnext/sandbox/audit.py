"""Independent DB assertions inside the disposable ERP container, not the adapter."""
import json
from decimal import Decimal
from cost_audit import verify_cost_document
from pathlib import Path
import frappe
import erpnext
from seed import COMPANY, USER
from lab_runtime import lab_session, phase, run_stage
from lab_permissions import verify_account_reference
from cost_fixtures import ACCEPTANCE_CASES, TAX_ACCOUNT, FREIGHT_ACCOUNT


def main():
    with lab_session():
        phase('database-evidence')
        assert frappe.db.get_value('Company', COMPANY, 'default_currency') == 'CNY'
        assert frappe.db.get_single_value('System Settings', 'currency_precision') == '2'
        assert frappe.db.get_single_value('System Settings', 'float_precision') == '6'
        assert frappe.db.get_single_value('System Settings', 'rounding_method') == 'Commercial Rounding'
        ops=json.loads(Path('/tmp/pf-erp-result.json').read_text())['operations']
        assert len(ops)==len(ACCEPTANCE_CASES)
        assert [op['scenario'] for op in ops] == list(ACCEPTANCE_CASES)
        rows=frappe.get_all('Supplier Quotation',fields=['name','docstatus','grand_total','custom_procureflow_operation_key','custom_procureflow_snapshot_hash'])
        assert len(rows)==len(ACCEPTANCE_CASES) and frappe.db.count('Purchase Order')==0
        for op in ops:
            matches=[r for r in rows if r.custom_procureflow_operation_key==op['operation_id']]
            assert len(matches)==1
            r=matches[0]
            assert r.name==op['remote_id'] and r.docstatus==0
            assert Decimal(str(r.grand_total)) == Decimal(ACCEPTANCE_CASES[op['scenario']]['total'])
            verify_cost_document(frappe.get_doc('Supplier Quotation', r.name).as_dict(), ACCEPTANCE_CASES[op['scenario']])
            assert r.custom_procureflow_snapshot_hash==op['snapshot_hash']
        indexes=frappe.db.sql('SHOW INDEX FROM `tabSupplier Quotation`',as_dict=True)
        assert any(r.Column_name=='custom_procureflow_operation_key' and r.Non_unique==0 for r in indexes)
        # Prove the unique constraint is active, then roll back this test write.
        frappe.db.sql('SAVEPOINT pf_unique_probe')
        duplicate_blocked=False
        try:
            frappe.db.sql('UPDATE `tabSupplier Quotation` SET custom_procureflow_operation_key=%s WHERE name=%s',
                (ops[0]['operation_id'],ops[1]['remote_id']))
        except Exception as error:
            duplicate_blocked='1062' in str(error) or 'Duplicate' in type(error).__name__
        finally:
            frappe.db.sql('ROLLBACK TO SAVEPOINT pf_unique_probe')
        assert duplicate_blocked
        denied={p:not frappe.has_permission('Supplier Quotation',p,user=USER) for p in ('submit','cancel','delete')}
        assert all(denied.values()) and not frappe.has_permission('Purchase Order','create',user=USER)
        phase('account-reference-permissions')
        account = frappe.db.get_value('Company', COMPANY, 'default_payable_account')
        reference_permissions = verify_account_reference(frappe.has_permission, USER, account)
        cost_reference_permissions = {kind: verify_account_reference(frappe.has_permission, USER, account)
            for kind, account in [('tax', TAX_ACCOUNT), ('freight', FREIGHT_ACCOUNT)]}
        result={'status':'passed','erpnext_version':erpnext.__version__,'frappe_version':frappe.__version__,
            'database_version':frappe.db.sql('SELECT VERSION()')[0][0],
            'currency_precision':'2', 'float_precision':'6', 'rounding_method':'Commercial Rounding',
            'draft_count':len(ACCEPTANCE_CASES),'purchase_order_count':0,'submitted_count':0,
            'remote_unique_index_present':True,'duplicate_key_update_rejected_and_rolled_back':True,
            'reference_permissions':reference_permissions, 'cost_reference_permissions':cost_reference_permissions,
            'restricted_permissions':denied,'adapter_readback_independently_checked':True, 'cost_components_independently_checked':True}
        frappe.db.rollback()
        return result

if __name__ == '__main__':
    raise SystemExit(run_stage('database-audit', main))
