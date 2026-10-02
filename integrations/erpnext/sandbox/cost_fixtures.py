"""Synthetic CI expectations, independently specified (never adapter-derived)."""
COST_CASES = {
    'normal': dict(unit_price='1200.00', tax_mode='included', tax_rate='0.13',
        shipping_cost='800.00', discount='0.00', goods='24000.00', net='21238.94',
        tax_before='2761.06', tax_after='2761.06', total='24800.00'),
    'lost-receipt': dict(unit_price='1180.00', tax_mode='included', tax_rate='0.13',
        shipping_cost='600.00', discount='0.00', goods='23600.00', net='20884.96',
        tax_before='2715.04', tax_after='2715.04', total='24200.00'),
    'excluded-discount': dict(unit_price='100.00', tax_mode='excluded', tax_rate='0.13',
        shipping_cost='80.00', discount='100.00', goods='2000.00', net='1900.00',
        tax_before='247.00', tax_after='247.00', total='2227.00'),
    'included-discount': dict(unit_price='113.00', tax_mode='included', tax_rate='0.13',
        shipping_cost='80.00', discount='113.00', goods='2260.00', net='1900.00',
        tax_before='260.00', tax_after='247.00', total='2227.00'),
}
TAX_ACCOUNT = 'PF Synthetic Goods Tax - PFL'
FREIGHT_ACCOUNT = 'PF Synthetic Gross Freight - PFL'
