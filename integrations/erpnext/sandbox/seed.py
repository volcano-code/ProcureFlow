"""Run ONLY inside the freshly created pf-erp-test.local container.

Creates synthetic master data and a restricted integration role. Credentials are
written to a private /tmp file for docker cp; never printed, logged, or committed.
No quotation/PO is created by this seed. No external-account credentials used.
"""
import datetime
import json
import os
from pathlib import Path
import secrets
import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SITE = 'pf-erp-test.local'
COMPANY = 'ProcureFlow Sandbox'
USER = 'pf-integration@example.invalid'
ROLE = 'ProcureFlow Draft Integration'


def connect_lab():
    if os.environ.get('PF_EPHEMERAL_ERP') != '1' or len(os.environ.get('PF_EPHEMERAL_NONCE','')) != 64:
        raise RuntimeError('EPHEMERAL_LAB_ONLY')
    frappe.init(site=SITE, sites_path='/home/frappe/frappe-bench/sites')
    frappe.connect()
    if frappe.conf.get('procureflow_test_nonce') != os.environ['PF_EPHEMERAL_NONCE']:
        raise RuntimeError('LAB_MARKER_MISMATCH')
    frappe.set_user('Administrator')


def main():
    connect_lab()
    try:
        if frappe.db.exists('Company', COMPANY) or frappe.db.exists('User', USER):
            raise RuntimeError('REFUSE_ALREADY_SEEDED_LAB')
        today = datetime.date.today(); year = str(today.year)
        if not frappe.db.exists('Fiscal Year', year):
            frappe.get_doc({'doctype':'Fiscal Year','year':year,
                'year_start_date':f'{year}-01-01','year_end_date':f'{year}-12-31'}).insert()
        frappe.get_doc({'doctype':'Company','company_name':COMPANY,'abbr':'PFL',
            'default_currency':'CNY','country':'China',
            'create_chart_of_accounts_based_on':'Standard Template', 'chart_of_accounts':'Standard'}).insert()
        if not frappe.db.exists('UOM','EA'):
            frappe.get_doc({'doctype':'UOM','uom_name':'EA','must_be_whole_number':1}).insert()
        frappe.get_doc({'doctype':'Supplier','supplier_name':'PF Synthetic Supplier',
            'supplier_type':'Company','supplier_group':'All Supplier Groups'}).insert()
        supplier = frappe.db.get_value('Supplier', {'supplier_name':'PF Synthetic Supplier'}, 'name')
        frappe.get_doc({'doctype':'Item','item_code':'PF-SANDBOX-ITEM','item_name':'Synthetic Test Item',
            'item_group':'All Item Groups','stock_uom':'EA','is_stock_item':0,'is_purchase_item':1}).insert()
        create_custom_fields({'Supplier Quotation':[
            {'fieldname':'custom_procureflow_operation_key','label':'ProcureFlow operation', 'fieldtype':'Data','unique':1,'no_copy':1},
            {'fieldname':'custom_procureflow_snapshot_hash','label':'ProcureFlow snapshot', 'fieldtype':'Data','no_copy':1}
        ]})
        frappe.get_doc({'doctype':'Role','role_name':ROLE,'desk_access':0}).insert()
        for dt in ('Supplier Quotation','Supplier','Company','Item','UOM','Currency','Custom Field','Price List'):
            add_permission(dt,ROLE,ptype='read')
        for right in ('create','write'):
            update_permission_property('Supplier Quotation',ROLE,0,right,1)
        # Explicitly no submit/cancel/delete or purchase-order permissions.
        key, secret = secrets.token_hex(15), secrets.token_hex(32)
        user = frappe.get_doc({'doctype':'User','email':USER,'first_name':'PF Integration',
            'enabled':1,'send_welcome_email':0,'user_type':'System User',
            'roles':[{'role':ROLE}], 'api_key':key,'api_secret':secret})
        user.insert()
        frappe.clear_cache(user=USER)
        frappe.db.commit()
        permissions = {p:bool(frappe.has_permission('Supplier Quotation',p,user=USER)) for p in ('read','create','write','submit','cancel','delete')}
        if not all(permissions[p] for p in ('read','create','write')) or any(permissions[p] for p in ('submit','cancel','delete')):
            raise RuntimeError('DRAFT_ROLE_NOT_RESTRICTED')
        if frappe.has_permission('Purchase Order','create',user=USER):
            raise RuntimeError('UNEXPECTED_PO_PERMISSION')
        path = Path('/tmp/pf-erp-sandbox-credentials.json')
        fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'site':SITE,'nonce':os.environ['PF_EPHEMERAL_NONCE'],
                'api_key':key,'api_secret':secret,'company':COMPANY,'user':USER,
                'supplier':supplier,'sku':'PF-SANDBOX-ITEM','permissions':permissions},f)
        print('Synthetic Company/Supplier/Item and draft-only integration identity created; credentials written privately.')
    finally:
        frappe.destroy()

if __name__ == '__main__': main()
