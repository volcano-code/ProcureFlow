"""Fixed bounded pilot gate contract, independent of the eight-case static gate."""
SCENARIOS = ('csv-excluded-discount', 'xlsx-included-discount-lost-receipt')
DENIALS = ('buyer-revocation', 'approver-revocation', 'approver-membership-expiry')
BROWSER_CHECKS = ('independent_contexts', 'distinct_users', 'native_invitation_login',
    'ordinary_table_import', 'explicit_quote_confirmation', 'independent_approval',
    'enqueue_only', 'same_origin_proxy', 'no_browser_credential_storage')
STEPS = ('production_next_build', 'dedicated_identity_get_only_preflight',
    'native_csv_browser_to_worker_draft', 'native_xlsx_lost_receipt_read_only_after_logout',
    'buyer_revocation_denies_restarted_worker_first_write', 'approver_revocation_denies_restarted_worker_first_write',
    'approver_membership_expiry_denies_restarted_worker_first_write', 'independent_business_database_audit')
