# Synthetic ordinary quote tables

Both files are generated synthetic data, not supplier records. In the native Next workbench create request SKU STAND-01, quantity20, currency CNY, UOM EA, budget30000, deadline14 days. Choose 导入普通表格. CSV: sheet CSV/header1/row2. XLSX: sheet 报价/header1/row2. Verify each suggested field mapping explicitly, preview the source coordinates and confirm import. The quote remains unconfirmed until the normal quote confirmation. Values use SUP-A and produce total24800 CNY under the baseline policy.

The explanation worksheet demonstrates explicit worksheet selection. Formulas, missing freight, wrong mappings and duplicate rows are generated in test_table_imports.py/test_tabular_parser.py rather than stored as real supplier data.
