/** UI lifecycle guards and pure mapping helpers. The server remains authoritative for parsing and CAS. */
export const TABLE_FIELDS = [
  ['supplier_id','供应商编码 / Supplier ID'], ['sku','型号 / SKU'], ['quantity','数量 / Quantity'],
  ['uom','单位 / UOM'], ['unit_price','单价 / Unit price'], ['tax_mode','税价模式 / Tax mode'],
  ['tax_rate','税率 / Tax rate'], ['shipping_cost','最终含税运费 / Freight'], ['discount','商品折扣额 / Discount'],
  ['delivery_days','交期天数 / Lead time'], ['currency','币种 / Currency'],
];
export const TABLE_MAX_ROWS = 20;
export const TABLE_HEADER_FIELDS = ['supplier_id','currency','shipping_cost'].map(field => TABLE_FIELDS.find(([key]) => key === field));
export const TABLE_LINE_FIELDS = TABLE_FIELDS.filter(([field]) => !TABLE_HEADER_FIELDS.some(([key]) => key === field));
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
export function selectedRows(selection) {
  return Array.isArray(selection.rows) ? [...selection.rows] : selection.row === undefined ? [] : [selection.row];
}
function rowSelectionProblems(selection) {
  const hasRow = Object.hasOwn(selection,'row'), hasRows = Object.hasOwn(selection,'rows');
  if (hasRow === hasRows) return ['请仅选择单行 row 或多行 rows，不能混用或同时省略'];
  if (hasRows && !Array.isArray(selection.rows)) return ['报价数据行必须是行号列表'];
  const rows = selectedRows(selection), problems = [];
  if (!rows.length || rows.length > TABLE_MAX_ROWS) problems.push(`请选择 1–${TABLE_MAX_ROWS} 个报价数据行`);
  if (rows.some(row => !Number.isSafeInteger(row) || row < 1)) problems.push('报价数据行必须使用有效整数行号');
  if (new Set(rows).size !== rows.length) problems.push('报价数据行不能重复');
  return problems;
}
export function mappingProblems(selection, sheet) {
  if (!sheet || selection.sheet !== sheet.name) return ['请选择工作表'];
  const header = sheet.rows.find(row => row.row === selection.header_row);
  const problems = rowSelectionProblems(selection), rows = selectedRows(selection);
  if (!header) problems.push('请选择有效表头行');
  if (rows.some(number => !sheet.rows.some(row => row.row === number) || number <= selection.header_row)) {
    problems.push('每个报价数据行必须存在且位于表头行之后');
  }
  const available = new Set(sheet.rows.flatMap(row => row.cells.map(cell => cell.column)));
  const columns = Object.values(compactMapping(selection.mapping));
  if (!columns.length) problems.push('请至少映射一个字段');
  if (columns.some(column => !available.has(column))) problems.push('映射列不在当前工作表中');
  if (new Set(columns).size !== columns.length) problems.push('同一列不能重复映射到多个字段');
  return problems;
}
export function selectionForSheet(sheet, multiItem = false) {
  const header = sheet.suggested_header_row ?? sheet.rows[0]?.row ?? 1;
  const first = sheet.rows.find(row => row.row > header)?.row;
  return {sheet:sheet.name,header_row:header,...(multiItem ? {rows:first === undefined ? [] : [first]} : {row:first ?? header}),
    mapping:compactMapping(sheet.suggested_mapping || {})};
}
export function previewBody(revision, selection) {
  const problems = rowSelectionProblems(selection);
  if (problems.length) throw new RangeError(problems.join('；'));
  return {expected_revision:revision,sheet:selection.sheet,header_row:selection.header_row,
    ...(Object.hasOwn(selection,'rows') ? {rows:[...selection.rows]} : {row:selection.row}),
    mapping:compactMapping(selection.mapping)};
}
/** Preserve unknown values and use the server's flat evidence paths without inferring defaults. */
export function previewSections(values) {
  const fields = (items, source, prefix = '') => items.map(([field,label]) => ({field,label,path:`${prefix}${field}`,value:source[field]}));
  if (!Array.isArray(values.lines)) return [{id:'quote',label:null,fields:fields(TABLE_FIELDS,values)}];
  return [
    {id:'header',label:'报价表头 · 运费只计一次',fields:fields(TABLE_HEADER_FIELDS,values)},
    ...values.lines.map((line,index) => ({id:`line-${index}`,label:`商品行 ${index+1}`,
      fields:fields(TABLE_LINE_FIELDS,line,`lines.${index}.`)})),
  ];
}
/** Header fields may share a normalized value, but every original source row remains visible. */
export function previewSources(source) {
  if (!source) return [];
  return Array.isArray(source.sources) && source.sources.length ? [...source.sources] : [source];
}
