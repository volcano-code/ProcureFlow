import test from 'node:test';
import assert from 'node:assert/strict';
import {MAX_LINES,LINE_FIELDS,blankQuoteLine,requestLines,quoteLines,requestLineProblems,coverageProblems,requestPayload,quotePayload,lineEvidence} from '../lib/procurement-lines.mjs';
import {violationExplanation} from '../lib/policy.mjs';
import {attachEditorLifecycle} from '../lib/editor-lifecycle.mjs';
const request=[{sku:'A',quantity:'2',uom:'EA'},{sku:'B',quantity:'3',uom:'EA'}];
const line=(sku='A',quantity='2')=>({sku,quantity,uom:'EA',unit_price:'10.00',tax_mode:'included',tax_rate:'0',discount:'0',delivery_days:7});
const form=entries=>{const result=new FormData();for(const [key,value]of Object.entries(entries))result.set(key,String(value));return result;};
const headers={title:'Fixture basket',budget:'1000.00',max_delivery_days:'14'};

test('request normalization preserves legacy scalar shape and explicit line order',()=>{
  assert.deepEqual(requestLines({sku:'Old',quantity:'20',uom:'EA'}),[{sku:'Old',quantity:'20',uom:'EA'}]);
  const normalized=requestLines({lines:request});assert.deepEqual(normalized,request);normalized[0].sku='changed';assert.equal(request[0].sku,'A');
});
test('legacy request submission never emits lines; decimal quantity remains a string',()=>{
  const body=requestPayload(form(headers),[request[0]],false);
  assert.deepEqual(body,{title:'Fixture basket',budget:'1000.00',max_delivery_days:14,currency:'CNY',sku:'A',quantity:'2',uom:'EA'});
});
test('multi request submission never emits scalar sku quantity or uom',()=>{
  const body=requestPayload(form(headers),request,true);assert.deepEqual(body.lines,request);
  for(const key of ['sku','quantity','uom'])assert.equal(Object.hasOwn(body,key),false);
});
test('bounded request rejects empty and over-limit line lists',()=>{
  assert.match(requestLineProblems([]).join(),/1–20/);
  assert.match(requestLineProblems(Array.from({length:MAX_LINES+1},(_,i)=>({sku:String(i),quantity:'1',uom:'EA'}))).join(),/1–20/);
  assert.deepEqual(requestLineProblems(Array.from({length:MAX_LINES},(_,i)=>({sku:String(i),quantity:'1',uom:'EA'}))),[]);
});
test('request lines require trimmed unique case-sensitive SKU and supported quantity/UOM',()=>{
  assert.match(requestLineProblems([{...request[0],sku:' A '},request[0]]).join(),/重复型号/);
  assert.deepEqual(requestLineProblems([{...request[0],sku:'a'},request[0]]),[]);
  for(const quantity of ['1E+3','1e-3','.5','1.0000'])assert.deepEqual(requestLineProblems([{...request[0],quantity}]),[]);
  assert.match(requestLineProblems([{...request[0],quantity:'1e-4'}]).join(),/数量/);
  for(const quantity of ['0','-1','NaN','Infinity','1.0001','1000001',''])assert.match(requestLineProblems([{...request[0],quantity}]).join(),/数量/);
  assert.match(requestLineProblems([{...request[0],uom:'KG'}]).join(),/EA/);
  assert.throws(()=>requestPayload(form(headers),[{...request[0],quantity:'0'}],true),/数量/);
});
test('blank quote line never invents zero values, SKU, EA or lead time',()=>{
  const blank=blankQuoteLine();assert.deepEqual(Object.keys(blank),LINE_FIELDS);
  assert.equal(blank.tax_mode,'unknown');for(const field of LINE_FIELDS.filter(field=>field!=='tax_mode'))assert.equal(blank[field],null);
});
test('quote normalization retains saved source order and partial nulls',()=>{
  const values={lines:[line('B','3'),blankQuoteLine()]};const result=quoteLines(values);
  assert.deepEqual(result,values.lines);result[0].sku='changed';assert.equal(values.lines[0].sku,'B');
  assert.deepEqual(quoteLines(line()),[line()]);
});
test('coverage can match reordered supplier lines without coercing missing quantities',()=>{
  assert.deepEqual(coverageProblems(request,[line('B','3'),line()]),[]);
  assert.match(coverageProblems(request,[{...line(),quantity:null},line('B','3')]).join(),/数量不一致或未知：A/);
});
test('coverage shows partial, unexpected, duplicate and unknown line SKU independently',()=>{
  const issues=coverageProblems(request,[line(),line(),{...line('C','1'),unit_price:null},blankQuoteLine()]);
  for(const phrase of ['重复型号：A','缺少需求型号：B','需求之外的型号：C','型号未知','单价未知'])assert.ok(issues.some(issue=>issue.includes(phrase)),phrase);
});
test('coverage identifies per-line unknown tax, discount, delivery and missing excluded rate',()=>{
  const unknown={...line(),tax_mode:'unknown',discount:null,delivery_days:null};
  const issues=coverageProblems([request[0]],[unknown]).join();
  for(const phrase of ['税价模式未知','商品折扣额未知','交期天数未知'])assert.ok(issues.includes(phrase),phrase);
  assert.match(coverageProblems([request[0]],[{...line(),tax_mode:'excluded',tax_rate:null}]).join(),/缺少税率/);
});
test('quote scalar payload retains legacy fields and explicit zero strings',()=>{
  const body=quotePayload(form({...line(),supplier_id:'S',currency:'CNY',shipping_cost:'0'}),false,1);
  assert.equal(body.shipping_cost,'0');assert.equal(body.discount,'0');assert.equal(body.delivery_days,7);assert.equal(Object.hasOwn(body,'lines'),false);
});
test('multi quote payload has shared headers once and preserves every decimal string',()=>{
  const values={supplier_id:'S',currency:'CNY',shipping_cost:'7.10'};
  [line(),line('B','3')].forEach((value,index)=>Object.entries(value).forEach(([key,item])=>values[`lines.${index}.${key}`]=item));
  const body=quotePayload(form(values),true,2);
  assert.equal(body.shipping_cost,'7.10');assert.deepEqual(body.lines,[line(),line('B','3')]);
  for(const key of LINE_FIELDS)assert.equal(Object.hasOwn(body,key),false);
  for(const item of body.lines)assert.equal(Object.hasOwn(item,'shipping_cost'),false);
});
test('empty multi quote fields stay null and tax stays unknown rather than zero',()=>{
  const body=quotePayload(form({supplier_id:'S',currency:'CNY'}),true,1);
  assert.equal(body.shipping_cost,null);assert.deepEqual(body.lines,[blankQuoteLine()]);
});
test('quote payload rejects unbounded or invalid line counts',()=>{
  for(const count of [0,21,1.2,NaN])assert.throws(()=>quotePayload(form({}),true,count),/1–20/);
});
test('line evidence looks up only exact saved path and never falls back to another item',()=>{
  const evidence={'lines.0.unit_price':{kind:'cell',text:'10'},unit_price:{kind:'text',text:'unrelated'}};
  assert.equal(lineEvidence(evidence,0,'unit_price'),evidence['lines.0.unit_price']);
  assert.equal(lineEvidence(evidence,1,'unit_price').kind,'unknown');
  assert.equal(lineEvidence(undefined,0,'sku').kind,'unknown');
});
test('line violations identify the exact item and full coverage failures',()=>{
  assert.match(violationExplanation('LINE_2_QUANTITY_MISMATCH'),/物料 2.*数量/);
  assert.match(violationExplanation('MISSING_REQUEST_LINES'),/全部需求物料/);
  assert.match(violationExplanation('UNEXPECTED_QUOTE_LINES'),/需求之外/);
});

test('legacy submission cannot silently discard additional lines',()=>{
  assert.throws(()=>requestPayload(form(headers),request,false),/只能提交一项/);
  assert.throws(()=>quotePayload(form({}),false,2),/只能提交一项/);
});

const keyEvent=(key='Escape')=>Object.assign(new Event('keydown',{cancelable:true}),{key});
test('request and quote editors open nonmodally so pending writes cannot make logout inert',()=>{
  const events=new EventTarget();let shown=0;
  const dialog={open:false,show(){shown++;this.open=true;},showModal(){assert.fail('Global logout must stay reachable');}};
  const dispose=attachEditorLifecycle(dialog,()=>{},()=>true,events);
  assert.equal(shown,1);assert.equal(dialog.open,true);dispose();
});
test('editor Escape and navigation preserve live busy guards without auto-saving',()=>{
  const events=new EventTarget();let busy=true,closed=0;
  const dispose=attachEditorLifecycle({open:true},()=>closed++,()=>busy,events);
  const heldEscape=keyEvent();events.dispatchEvent(heldEscape);events.dispatchEvent(new Event('popstate'));
  assert.equal(heldEscape.defaultPrevented,true);assert.equal(closed,0);
  busy=false;events.dispatchEvent(keyEvent());assert.equal(closed,1);
  events.dispatchEvent(new Event('popstate'));assert.equal(closed,2);dispose();
});
test('editor ignores other keys and already handled Escape; unmount removes all listeners',()=>{
  const events=new EventTarget();let closed=0;
  const dispose=attachEditorLifecycle({open:true},()=>closed++,()=>false,events);
  events.dispatchEvent(keyEvent('Enter'));const handled=keyEvent();handled.preventDefault();events.dispatchEvent(handled);
  assert.equal(closed,0);dispose();
  events.dispatchEvent(keyEvent());events.dispatchEvent(new Event('popstate'));assert.equal(closed,0);
});
test('an already open editor is never reopened and the newest close callback remains usable',()=>{
  const events=new EventTarget();let closed='';let current=()=>{closed='old';};
  const dispose=attachEditorLifecycle({open:true,show(){assert.fail('Already open');}},()=>current(),()=>false,events);
  current=()=>{closed='current';};events.dispatchEvent(keyEvent());assert.equal(closed,'current');dispose();
});
