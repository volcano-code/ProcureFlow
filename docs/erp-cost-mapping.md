# Bounded ERPNext draft cost mapping

This increment is code and offline-test evidence until the **new exact commit** passes the disposable ERPNext workflow. Earlier two-draft zero-cost CI results do not validate this mapping. No production ERP was accessed; no documents were submitted and no Purchase Orders were authorized. The existing CI negative PO-create probe still must be denied.

## Commercial boundary

The adapter maps one CNY Supplier Quotation item, EA, with a positive integral quantity (at most 1,000,000). Unit price, freight, discount, pre-discount extended goods, and final total must each be at most CNY 1,000,000.00. Prices, absolute goods discounts, and gross freight have at most two decimal places. A known tax mode and explicit rate in [0, 1] are required, including for inclusive pricing. Domain comparisons remain more general; comparability alone does not guarantee ERP representability.

- Tax is one `On Net Total` / `Add` / `Total` row. An included goods price uses `included_in_print_rate=1`; an excluded goods price uses 0
- Freight is a distinct `Actual` / `Add` / `Total` row, with `included_in_print_rate=0`. It is a final gross charge and never receives an extra goods-tax percentage
- Absolute goods discount uses `Net Total` for excluded prices, and `Grand Total` for included prices. ERPNext excludes the Actual freight row from the latter discount base
- Zero-rate tax and zero freight omit their rows. No tax, shipping, item-tax, or pricing-rule templates are accepted; no line-level discounts, extra rows, deductions, compound taxes, foreign exchange, stock valuation, withholding, or payment mapping is supported
- Readback requires two-decimal monetary and net-rate values matching the approved Decimal calculation exactly. No monetary tolerance is used, and a correct grand total cannot compensate for incorrect components

The inclusive-discount path is intentionally narrower than all arithmetic-valid quotes. It rounds the original goods net amount to cents, then distributes the gross goods discount on that rounded net. Both original and discounted net-plus-tax must equal their exact gross goods amounts. Otherwise `ERP_INCLUSIVE_ROUNDING_UNSUPPORTED` stops before any network write. For example, 0.05 of goods at 13% inclusive tax with a 0.01 discount is rejected; ERP's rounding correction must not conceal a component mismatch. With no discount, inclusive tax is calculated from the original unrounded net base.

The ProcureFlow API and approval snapshot retain decimal strings. The ERP REST boundary serializes numeric fields as exact JSON number tokens directly from Decimal, without a binary-float conversion; Frappe header validation can treat the string "0" as truthy before coercion.

ERPNext uses configurable precision and floating arithmetic. The sandbox pins currency precision 2, float precision 6, and Commercial Rounding; both seed and independent database evidence must report these settings. Exact persisted component comparisons remain mandatory even under that configuration. Other site versions, regional apps, defaults, or numeric edge cases can reject a write or leave a draft requiring human review; offline tests do not establish universal monetary equivalence.

## Explicit account configuration and approval binding

`ERP_TAX_ACCOUNT` and `ERP_FREIGHT_ACCOUNT` must name two distinct, existing accounts of the configured company when their corresponding amounts are nonzero. The application does not create accounts, infer tax accounting treatment, or grant permissions. Account names and `single-line-costs-v1` are included in the immutable approval snapshot; changing either invalidates execution/recovery before networking. The ERP company and endpoint checks remain in force. A GET-only check requires the company’s persisted default currency to be CNY before creation or lookup; conversion rate 1 cannot be used as an implicit FX assumption.

The sandbox seeds two clearly synthetic Asset/Tax leaf accounts solely to exercise draft commercial totals. This is not a production ledger design or a recommendation about tax recoverability or freight capitalization. Effective Account rights on payable, tax, and freight must remain select-only; the evidence checker rejects missing cost-account checks or any expanded right. The integration role is unchanged: draft Supplier Quotation read/create/write only, no submit/cancel/delete and no Purchase Order create.

Historical frozen snapshots missing the new mapping-version or account fields are blocked by execution/verification target checks. They are not rewritten or silently certified under the new mapping. A new compatible proposal and approval are required; completed historical operation status remains history.

## Independent checks and four real-CI scenarios

The adapter always GETs the persisted document after POST, checks operation key, snapshot hash, draft status, identity, item quantity/rate/amount/net amount/net rate, discount fields, account names, tax semantics, original and after-discount tax, cumulative row totals, freight, and exact total. Lost responses reconcile with read-only lookup. A different cost row with the same grand total fails.

The disposable CI gate now requires exactly four unique draft operations and exactly four POST attempts:

1. Normal A: 20 × 1,200.00 inclusive at 13%, freight 800.00, no discount → 24,800.00 (net 21,238.94; tax 2,761.06)
2. C with lost-receipt recovery: 20 × 1,180.00 inclusive at 13%, freight 600.00, no discount → 24,200.00 (net 20,884.96; tax 2,715.04)
3. Excluded-tax discount: 20 × 100.00, discount 100.00, 13% goods tax, gross freight 80.00 → 2,227.00 (net 1,900.00; tax 247.00)
4. Included-tax discount: 20 × 113.00 inclusive at 13%, discount 113.00, gross freight 80.00 → 2,227.00 (net 1,900.00; original tax 260.00; discounted tax 247.00)

Each case runs approval, Worker execution, independent GET verification, and replay. A separate in-container database audit uses fixed expected values without importing the adapter. It checks all item/charge components and totals, zero POs/submissions, unique-key enforcement, and unchanged restricted permissions. Both SQLite and PostgreSQL business-database matrix legs must pass the updated evidence checker; historical two-draft artifacts deliberately fail it.

Local reproduction uses the existing guarded procedure in [stage6](stage6-erp-sandbox.md), followed by the permission-probe and evidence commands in the current workflow. It requires a fresh isolated Docker project. Without Docker, only offline adapter and evidence contracts run; do not label their fixtures as real ERP results.

## Upstream basis

The mappings were checked against tagged ERPNext v16.36.0 code, not inferred from field labels:

- [Tax calculation, inclusive-rate handling, discount distribution, and Actual-row exclusion](https://github.com/frappe/erpnext/blob/v16.36.0/erpnext/controllers/taxes_and_totals.py)
- [Account/row validation and Actual-rate normalization](https://github.com/frappe/erpnext/blob/v16.36.0/erpnext/controllers/accounts_controller.py)
- [Supplier Quotation fields](https://github.com/frappe/erpnext/blob/v16.36.0/erpnext/buying/doctype/supplier_quotation/supplier_quotation.json) and [item precision schema](https://github.com/frappe/erpnext/blob/v16.36.0/erpnext/buying/doctype/supplier_quotation_item/supplier_quotation_item.json)
- [Purchase tax/charge semantics](https://docs.frappe.io/erpnext/purchase-taxes-and-charges-template) and [valuation versus total](https://docs.frappe.io/erpnext/difference-in-total-and-valuation-in-tax-and-charges)

These sources establish intended semantics; only the updated disposable integration run can establish the real roundtrip result for this commit.
