"""Independent real ERP SQL audit for the synthetic candidate procurement gate."""
import json
from decimal import Decimal
from pathlib import Path
import frappe
from lab_runtime import lab_session, run_stage
from seed import USER, COMPANY

def main():
    with lab_session():
        record=json.loads(Path('/tmp/pf-candidate-result.json').read_text())
        assert record['status']=='passed' and record['real_model_used'] is False
        operations=record['operations'];assert len(operations)==3
        rows=frappe.get_all('Supplier Quotation',fields=['name','docstatus','grand_total',
            'supplier','company','currency','custom_procureflow_operation_key','custom_procureflow_snapshot_hash'])
        assert len(rows)==2 and frappe.db.count('Purchase Order')==0
        for op in operations[:2]:
            row,=[r for r in rows if r.custom_procureflow_operation_key==op['operation_id']]
            assert row.name==op['remote_id'] and row.docstatus==0 and row.custom_procureflow_snapshot_hash==op['snapshot_hash']
            assert row.company==COMPANY and row.currency=='CNY' and row.supplier=='PF Synthetic Supplier'
            assert Decimal(str(row.grand_total))==Decimal('2000.00')
            item,=frappe.get_all('Supplier Quotation Item',filters={'parent':row.name},fields=['item_code','qty','rate','uom'])
            assert item.item_code=='PF-SANDBOX-ITEM' and item.uom=='EA'
            assert Decimal(str(item.qty))==20 and Decimal(str(item.rate))==100
        assert not any(r.custom_procureflow_operation_key==operations[2]['operation_id'] for r in rows)
        indexes=frappe.db.sql('SHOW INDEX FROM `tabSupplier Quotation`',as_dict=True)
        assert any(r.Column_name=='custom_procureflow_operation_key' and r.Non_unique==0 for r in indexes)
        assert all(not frappe.has_permission('Supplier Quotation',p,user=USER) for p in ('submit','cancel','delete'))
        assert not frappe.has_permission('Purchase Order','create',user=USER)
        frappe.db.rollback()
        return {'source_tree':record['source_tree'],'operation_ids':[x['operation_id'] for x in operations[:2]],
            'draft_count':2,'purchase_order_count':0,'submitted_count':0,'contents_checked':True,
            'unique_index_checked':True,'permissions_checked':True,'revoked_operation_absent':True}

if __name__=='__main__':raise SystemExit(run_stage('database-audit',main))
