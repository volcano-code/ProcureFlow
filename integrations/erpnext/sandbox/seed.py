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
from erpnext.setup.setup_wizard.operations import install_fixtures

from lab_runtime import SITE, lab_session, phase, run_stage
from lab_permissions import verify_account_reference
from cost_fixtures import TAX_ACCOUNT, FREIGHT_ACCOUNT
COMPANY = 'ProcureFlow Sandbox'
USER = 'pf-integration@example.invalid'
ROLE = 'ProcureFlow Draft Integration'


def main():
    with lab_session():
        if frappe.db.exists('Company', COMPANY) or frappe.db.exists('User', USER):
            raise RuntimeError('REFUSE_ALREADY_SEEDED_LAB')
        # bench --install-app does not run the setup wizard's reference fixtures.
        # Use ERPNext's own initializer; do not ignore missing link validation.
        phase('reference-fixtures')
        install_fixtures.install(country='China')
        phase('fiscal-year')
        today = datetime.date.today(); year = str(today.year)
        if not frappe.db.exists('Fiscal Year', year):
            frappe.get_doc({'doctype':'Fiscal Year','year':year,
                'year_start_date':f'{year}-01-01','year_end_date':f'{year}-12-31'}).insert()
        phase('company')
        frappe.get_doc({'doctype':'Company','company_name':COMPANY,'abbr':'PFL',
            'default_currency':'CNY','country':'China',
            'create_chart_of_accounts_based_on':'Standard Template', 'chart_of_accounts':'Standard'}).insert()
        phase('company-defaults')
        install_fixtures.install_defaults(frappe._dict(currency='CNY', company_name=COMPANY))
        phase('cost-accounts')
        # Two distinct, non-posting lab fixtures. No accounting recommendation.
        parent = frappe.db.get_value('Account', {'company': COMPANY, 'root_type': 'Asset', 'is_group': 1, 'parent_account': ['is', 'not set']}, 'name')
        if not parent:
            raise RuntimeError('SYNTHETIC_ACCOUNT_PARENT_MISSING')
        for account in (TAX_ACCOUNT, FREIGHT_ACCOUNT):
            doc = frappe.get_doc({'doctype': 'Account', 'account_name': account.removesuffix(' - PFL'),
                'company': COMPANY, 'parent_account': parent, 'account_type': 'Tax',
                'account_currency': 'CNY', 'is_group': 0}).insert()
            if doc.name != account:
                raise RuntimeError('SYNTHETIC_ACCOUNT_NAME_MISMATCH')
        phase('cost-precision')
        frappe.db.set_single_value('System Settings', 'currency_precision', '2')
        frappe.db.set_single_value('System Settings', 'float_precision', '6')
        frappe.db.set_single_value('System Settings', 'rounding_method', 'Commercial Rounding')
        frappe.clear_cache()
        phase('uom')
        if not frappe.db.exists('UOM','EA'):
            frappe.get_doc({'doctype':'UOM','uom_name':'EA','must_be_whole_number':1}).insert()
        phase('supplier')
        frappe.get_doc({'doctype':'Supplier','supplier_name':'PF Synthetic Supplier',
            'supplier_type':'Company','supplier_group':'Local'}).insert()
        supplier = frappe.db.get_value('Supplier', {'supplier_name':'PF Synthetic Supplier'}, 'name')
        phase('item')
        for sku in ('PF-SANDBOX-ITEM', 'PF-SANDBOX-ITEM-2'):
            frappe.get_doc({'doctype':'Item','item_code':sku,'item_name':'Synthetic Test Item',
                'item_group':'Products','stock_uom':'EA','is_stock_item':0,'is_purchase_item':1}).insert()
        phase('custom-fields')
        create_custom_fields({'Supplier Quotation':[
            {'fieldname':'custom_procureflow_operation_key','label':'ProcureFlow operation', 'fieldtype':'Data','unique':1,'no_copy':1},
            {'fieldname':'custom_procureflow_snapshot_hash','label':'ProcureFlow snapshot', 'fieldtype':'Data','no_copy':1}
        ]})
        phase('role')
        # Frappe derives user_type from role desk access, not the User input alone.
        # Only the explicitly listed document permissions below are granted.
        frappe.get_doc({'doctype':'Role','role_name':ROLE,'desk_access':1}).insert()
        for dt in ('Supplier Quotation','Supplier','Company','Item','UOM','Currency','Custom Field','Price List'):
            add_permission(dt,ROLE,ptype='read')
        # ERPNext v16.36.0 party.get_party_account validates Account select/read
        # even for a Supplier Quotation. Select is sufficient; do NOT grant read.
        add_permission('Account', ROLE, ptype='select')
        # Custom DocPerm defaults read/export to 1 even for ptype='select'.
        # Revoke export before read; keep Frappe's validation enabled.
        update_permission_property('Account', ROLE, 0, 'export', 0)
        update_permission_property('Account', ROLE, 0, 'read', 0)
        for right in ('create','write'):
            update_permission_property('Supplier Quotation',ROLE,0,right,1)
        # Explicitly no submit/cancel/delete or purchase-order permissions.
        phase('integration-user')
        key, secret = secrets.token_hex(15), secrets.token_hex(32)
        user = frappe.get_doc({'doctype':'User','email':USER,'first_name':'PF Integration',
            'enabled':1,'send_welcome_email':0,'user_type':'System User',
            'roles':[{'role':ROLE}], 'api_key':key,'api_secret':secret})
        user.insert()
        frappe.clear_cache(user=USER)
        frappe.db.commit()
        phase('verify-permissions')
        if frappe.db.get_value('User', USER, 'user_type') != 'System User':
            raise RuntimeError('INTEGRATION_USER_TYPE_MISMATCH')
        permissions = {p:bool(frappe.has_permission('Supplier Quotation',p,user=USER)) for p in ('read','create','write','submit','cancel','delete')}
        if not all(permissions[p] for p in ('read','create','write')) or any(permissions[p] for p in ('submit','cancel','delete')):
            raise RuntimeError('DRAFT_ROLE_NOT_RESTRICTED')
        if frappe.has_permission('Purchase Order','create',user=USER):
            raise RuntimeError('UNEXPECTED_PO_PERMISSION')
        phase('account-reference-permissions')
        account = frappe.db.get_value('Company', COMPANY, 'default_payable_account')
        reference_permissions = verify_account_reference(frappe.has_permission, USER, account)
        cost_reference_permissions = {kind: verify_account_reference(frappe.has_permission, USER, account)
            for kind, account in [('tax', TAX_ACCOUNT), ('freight', FREIGHT_ACCOUNT)]}
        phase('private-credentials')
        path = Path('/tmp/pf-erp-sandbox-credentials.json')
        fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'site':SITE,'nonce':os.environ['PF_EPHEMERAL_NONCE'],
                'api_key':key,'api_secret':secret,'company':COMPANY,'user':USER,
                'supplier':supplier,'sku':'PF-SANDBOX-ITEM','multi_skus':['PF-SANDBOX-ITEM','PF-SANDBOX-ITEM-2'],'permissions':permissions},f)
        return {'synthetic_only': True, 'permissions': permissions,
            'synthetic_item_count': 2, 'multi_skus': ['PF-SANDBOX-ITEM', 'PF-SANDBOX-ITEM-2'],
            'reference_permissions': reference_permissions, 'cost_reference_permissions': cost_reference_permissions,
            'credentials_written_privately': True, 'site_context': 'bench-sites',
            'integration_user_type': 'System User',
            'currency_precision': '2', 'float_precision': '6', 'rounding_method': 'Commercial Rounding'}

if __name__ == '__main__':
    raise SystemExit(run_stage('seed', main))
