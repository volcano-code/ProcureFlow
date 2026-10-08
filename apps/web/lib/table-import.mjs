/** UI lifecycle guards and pure mapping helpers. The server remains authoritative for parsing and CAS. */
export const TABLE_FIELDS = [
  ['supplier_id','供应商编码 / Supplier ID'], ['sku','型号 / SKU'], ['quantity','数量 / Quantity'],
  ['uom','单位 / UOM'], ['unit_price','单价 / Unit price'], ['tax_mode','税价模式 / Tax mode'],
  ['tax_rate','税率 / Tax rate'], ['shipping_cost','最终含税运费 / Freight'], ['discount','商品折扣额 / Discount'],
  ['delivery_days','交期天数 / Lead time'], ['currency','币种 / Currency'],
];
export function createImportScope() {
  const controller = new AbortController(); let epoch = 0, pending = false, active = true;
  return {
    begin() { if (!active || pending) return null; pending = true; return {epoch:++epoch, signal:controller.signal}; },
    current(ticket) { return active && !!ticket && ticket.epoch === epoch && ticket.signal === controller.signal && !controller.signal.aborted; },
    finish(ticket) { if (this.current(ticket)) pending = false; },
    dispose() { active = false; pending = false; ++epoch; controller.abort(); },
  };
}
export function importReadOnly(status) {
  return status !== 'OPEN';
}
export function importExpired(expiresAt, now = Date.now()) {
  const expires = Date.parse(expiresAt); return !Number.isFinite(expires) || expires <= now;
}
export function compactMapping(mapping) {
  return Object.fromEntries(TABLE_FIELDS.flatMap(([field]) => typeof mapping[field] === 'string' && mapping[field]
    ? [[field,mapping[field]]] : []));
}
export function mappingProblems(selection, sheet) {
  if (!sheet || selection.sheet !== sheet.name) return ['请选择工作表'];
  const header = sheet.rows.find(row => row.row === selection.header_row);
  const data = sheet.rows.find(row => row.row === selection.row);
  const problems = [];
  if (!header) problems.push('请选择有效表头行');
  if (!data || selection.row <= selection.header_row) problems.push('报价数据行必须位于表头行之后');
  const available = new Set(sheet.rows.flatMap(row => row.cells.map(cell => cell.column)));
  const columns = Object.values(compactMapping(selection.mapping));
  if (!columns.length) problems.push('请至少映射一个字段');
  if (columns.some(column => !available.has(column))) problems.push('映射列不在当前工作表中');
  if (new Set(columns).size !== columns.length) problems.push('同一列不能重复映射到多个字段');
  return problems;
}
export function selectionForSheet(sheet) {
  const header = sheet.suggested_header_row ?? sheet.rows[0]?.row ?? 1;
  return {sheet:sheet.name,header_row:header,row:sheet.rows.find(row => row.row > header)?.row ?? header,
    mapping:compactMapping(sheet.suggested_mapping || {})};
}
export function previewBody(revision, selection) {
  return {expected_revision:revision,sheet:selection.sheet,header_row:selection.header_row,row:selection.row,
    mapping:compactMapping(selection.mapping)};
}
