/** Display/form helpers only. The server calculates money and decides eligibility. */
export const MAX_LINES = 20;
export const LINE_FIELDS = ['sku','quantity','uom','unit_price','tax_mode','tax_rate','discount','delivery_days'];
export const HEADER_FIELDS = ['supplier_id','currency','shipping_cost'];
export const FIELD_LABELS = {supplier_id:'供应商编码',sku:'型号',quantity:'数量',uom:'单位',unit_price:'单价',
  tax_mode:'税价模式',tax_rate:'税率',shipping_cost:'最终含税运费',discount:'商品折扣额',delivery_days:'交期天数',currency:'币种'};
export function blankQuoteLine() {
  return Object.fromEntries(LINE_FIELDS.map(field=>[field,field==='tax_mode'?'unknown':null]));
}
export function requestLines(request) {
  return request?.lines?.length ? request.lines.map(line=>({...line})) : [{sku:request?.sku??'STAND-01',quantity:request?.quantity??'20',uom:request?.uom??'EA'}];
}
export function quoteLines(values) {
  return values.lines?.length ? values.lines.map(line=>({...line})) : [Object.fromEntries(LINE_FIELDS.map(field=>[field,values[field]]))];
}
export function lineEvidence(evidence,index,field) {
  return evidence?.[`lines.${index}.${field}`] || {kind:'unknown',reason:'该物料字段没有原文支持，保持未知。'};
}
const text=value=>value==null?'':String(value).trim();
const validQuantity=value=>{
  if(!/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value))return false;
  const [mantissa,exponent='0']=value.toLowerCase().split('e');
  const scale=(mantissa.split('.')[1]||'').replace(/0+$/,'').length-Number(exponent);
  return Number.isFinite(Number(value))&&Number(value)>0&&Number(value)<=1000000&&scale<=3;
};
export function requestLineProblems(lines) {
  const issues=[];
  if(!Array.isArray(lines)||lines.length<1||lines.length>MAX_LINES)return ['采购需求必须包含 1–20 项物料'];
  const seen=new Set();
  lines.forEach((line,index)=>{
    const sku=text(line.sku),quantity=text(line.quantity);
    if(!sku||sku.length>80)issues.push(`第 ${index+1} 项：型号必须为 1–80 个字符`);
    else if(seen.has(sku))issues.push(`第 ${index+1} 项：重复型号 ${sku}`);
    seen.add(sku);
    if(!validQuantity(quantity))
      issues.push(`第 ${index+1} 项：数量须大于零、最多三位小数且不超过 1000000`);
    if(line.uom!=='EA')issues.push(`第 ${index+1} 项：当前仅支持 EA 单位`);
  });
  return issues;
}
export function coverageProblems(required,offered) {
  const issues=[],seen=new Set(),needed=new Set(required.map(line=>line.sku));
  if(offered.length<1||offered.length>MAX_LINES)issues.push('报价必须包含 1–20 项物料');
  offered.forEach((line,index)=>{
    const sku=text(line.sku);
    if(!sku){issues.push(`第 ${index+1} 项：型号未知，无法核验物料覆盖`);return;}
    if(seen.has(sku))issues.push(`重复型号：${sku}`);
    seen.add(sku);
    if(!needed.has(sku))issues.push(`需求之外的型号：${sku}`);
    const requested=required.find(item=>item.sku===sku);
    if(requested && (line.quantity==null||text(line.quantity)===''||Number(line.quantity)!==Number(requested.quantity)))issues.push(`数量不一致或未知：${sku}`);
    if(requested && text(line.uom).toUpperCase()!==requested.uom)issues.push(`单位不一致或未知：${sku}`);
    for(const field of ['unit_price','discount','delivery_days'])if(line[field]==null||text(line[field])==='')issues.push(`${sku}：${FIELD_LABELS[field]}未知`);
    if(!line.tax_mode||line.tax_mode==='unknown')issues.push(`${sku}：税价模式未知`);
    if(line.tax_mode==='excluded'&&(line.tax_rate==null||text(line.tax_rate)===''))issues.push(`${sku}：未税报价缺少税率`);
  });
  required.forEach(line=>{if(!seen.has(line.sku))issues.push(`缺少需求型号：${line.sku}`);});
  return issues;
}
export function requestPayload(form,lines,multiItem) {
  const problems=requestLineProblems(lines);if(!multiItem&&lines.length!==1)problems.push("单物料模式只能提交一项物料");if(problems.length)throw new Error(problems.join('；'));
  const common={title:text(form.get('title')),budget:text(form.get('budget')),max_delivery_days:Number(form.get('max_delivery_days')),currency:'CNY'};
  const normalized=lines.map(line=>({sku:text(line.sku),quantity:text(line.quantity),uom:'EA'}));
  return multiItem?{...common,lines:normalized}:{...common,...normalized[0]};
}
export function quotePayload(form,multiItem,lineCount) {
  const read=(field,prefix='')=>{const value=text(form.get(prefix+field));return value===''?(field==='tax_mode'?'unknown':null):field==='delivery_days'?Number(value):value;};
  const headers=Object.fromEntries(HEADER_FIELDS.map(field=>[field,read(field)]));
  if(!multiItem&&lineCount!==1)throw new Error('单物料模式只能提交一项物料');
  if(!multiItem)return {...headers,...Object.fromEntries(LINE_FIELDS.map(field=>[field,read(field)]))};
  if(!Number.isInteger(lineCount)||lineCount<1||lineCount>MAX_LINES)throw new Error('报价必须包含 1–20 项物料');
  return {...headers,lines:Array.from({length:lineCount},(_,index)=>Object.fromEntries(LINE_FIELDS.map(field=>[field,read(field,`lines.${index}.`)])))};
}
