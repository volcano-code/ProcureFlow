"""Independent DB assertions inside the disposable ERP container, not the adapter."""
import json
from pathlib import Path
import frappe
import erpnext
from seed import COMPANY, USER
from lab_runtime import lab_session, phase, run_stage
from lab_permissions import verify_account_reference


def main():
    with lab_session():
        phase('database-evidence')
        ops=json.loads(Path('/tmp/pf-erp-result.json').read_text())['operations']
        assert len(ops)==2
        rows=frappe.get_all('Supplier Quotation',fields=['name','docstatus','grand_total','custom_procureflow_operation_key','custom_procureflow_snapshot_hash'])
        assert len(rows)==2 and frappe.db.count('Purchase Order')==0
        for op in ops:
            matches=[r for r in rows if r.custom_procureflow_operation_key==op['operation_id']]
            assert len(matches)==1
            r=matches[0]
            assert r.name==op['remote_id'] and r.docstatus==0
            assert str(r.grand_total) in ('2000.0','2000.00','2000')
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
        result={'status':'passed','erpnext_version':erpnext.__version__,'frappe_version':frappe.__version__,
            'database_version':frappe.db.sql('SELECT VERSION()')[0][0],
            'draft_count':2,'purchase_order_count':0,'submitted_count':0,
            'remote_unique_index_present':True,'duplicate_key_update_rejected_and_rolled_back':True,
            'reference_permissions':reference_permissions,
            'restricted_permissions':denied,'adapter_readback_independently_checked':True}
        frappe.db.rollback()
        return result

if __name__ == '__main__':
    raise SystemExit(run_stage('database-audit', main))
