"""Independent ERP database cost contract. No adapter imports or remote writes."""
from decimal import Decimal
from cost_fixtures import TAX_ACCOUNT, FREIGHT_ACCOUNT
COMPANY = "ProcureFlow Sandbox"

def verify_cost_document(doc, expected):
    """Independent exact-cent DB audit; never trusts the POST or adapter proof."""
    number = lambda value: Decimal(str(value))
    assert doc['currency'] == 'CNY' and doc['company'] == COMPANY and doc['docstatus'] == 0
    for field, wanted in [('total', expected['goods']), ('net_total', expected['net']),
            ('grand_total', expected['total']), ('discount_amount', expected['discount']),
            ('additional_discount_percentage', '0'), ('conversion_rate', '1')]:
        assert number(doc[field]) == number(wanted), field
    assert doc['apply_discount_on'] == ('Grand Total' if expected['tax_mode'] == 'included' else 'Net Total')
    assert doc['disable_rounded_total'] == 1
    assert len(doc['items']) == 1
    item = doc['items'][0]
    assert item['item_code'] == 'PF-SANDBOX-ITEM' and item['uom'] == 'EA'
    assert number(item['qty']) == 20 and number(item['rate']) == number(expected['unit_price'])
    assert number(item['amount']) == number(expected['goods']) and number(item['net_amount']) == number(expected['net'])
    assert len(doc['taxes']) == 2
    tax, freight = doc['taxes']
    for row in (tax, freight):
        assert row['category'] == 'Total' and row['add_deduct_tax'] == 'Add'
        assert not row.get('row_id') and row.get('dont_recompute_tax') == 0
    assert tax['charge_type'] == 'On Net Total' and tax['account_head'] == TAX_ACCOUNT
    assert number(tax['rate']) == 13 and tax['included_in_print_rate'] == int(expected['tax_mode'] == 'included')
    assert number(tax['tax_amount']) == number(expected['tax_before'])
    assert number(tax['tax_amount_after_discount_amount']) == number(expected['tax_after'])
    assert freight['charge_type'] == 'Actual' and freight['account_head'] == FREIGHT_ACCOUNT
    assert (freight['rate'] is None or number(freight['rate']) == 0) and freight['included_in_print_rate'] == 0
    assert number(freight['tax_amount']) == number(expected['shipping_cost'])
    assert number(freight['tax_amount_after_discount_amount']) == number(expected['shipping_cost'])
    assert number(tax['total']) == number(expected['net']) + number(expected['tax_after'])
    assert number(freight['total']) == number(expected['total'])
    assert all(not doc.get(key) for key in ('taxes_and_charges', 'shipping_rule', 'tax_category'))
    assert not item.get('item_tax_template') and not item.get('pricing_rules') and not doc.get('pricing_rules')
    assert number(item['discount_percentage']) == 0 and number(item['discount_amount']) == 0
    assert number(item['net_rate']) == (number(expected['net']) / 20).quantize(Decimal('0.01'), rounding='ROUND_HALF_UP')
    assert number(item['price_list_rate']) == number(expected['unit_price'])
    assert number(doc['total_taxes_and_charges']) == number(expected['tax_after']) + number(expected['shipping_cost'])
