import test from 'node:test';
import assert from 'node:assert/strict';
import {TABLE_MAX_ROWS,TABLE_FIELDS,TABLE_HEADER_FIELDS,TABLE_LINE_FIELDS,createImportScope,importExpired,importReadOnly,compactMapping,mappingProblems,selectedRows,selectionForSheet,previewBody,previewSections,previewSources} from '../lib/table-import.mjs';
const sheet = {name:'供应商报价',suggested_header_row:2,suggested_mapping:{supplier_id:'A',unit_price:'B'},rows:[
  {row:1,cells:[{column:'A',cell:'A1',value:'报价单'}]},
  {row:2,cells:[{column:'A',cell:'A2',value:'供应商编码'},{column:'B',cell:'B2',value:'单价'}]},
  {row:3,cells:[{column:'A',cell:'A3',value:'SUP-A'},{column:'B',cell:'B3',value:'0'}]},
  {row:4,cells:[{column:'A',cell:'A4',value:'SUP-B'},{column:'B',cell:'B4',value:'=1+1',formula:true}]},
]};

test('a scope rejects repeated clicks synchronously and unlocks only the matching operation',()=>{
  const scope=createImportScope(),first=scope.begin();
  assert.ok(first);assert.equal(scope.begin(),null);assert.equal(scope.current(first),true);
  scope.finish(first);const second=scope.begin();assert.ok(second);assert.equal(scope.current(first),false);
  scope.finish(first);assert.equal(scope.begin(),null);scope.finish(second);assert.ok(scope.begin());
});
test('close, Escape, navigation and remount invalidate late async results',async()=>{
  const abandoned=createImportScope(),ticket=abandoned.begin();
  abandoned.dispose();assert.equal(ticket.signal.aborted,true);assert.equal(abandoned.current(ticket),false);
  assert.equal(abandoned.begin(),null);
  const reopened=createImportScope(),fresh=reopened.begin();
  assert.equal(reopened.current(fresh),true);assert.equal(reopened.current(ticket),false);assert.equal(abandoned.current(ticket),false);
});
test('expiry is fail-closed, including malformed and exact-boundary dates',()=>{
  const now=Date.parse('2026-10-03T15:00:00Z');
  assert.equal(importExpired('2026-10-03T15:01:00Z',now),false);
  for(const value of ['2026-10-03T15:00:00Z','2026-10-02T15:01:00Z','bad'])assert.equal(importExpired(value,now),true);
});
test('Chinese header suggestions preserve physical sheet, row and column coordinates',()=>{
  const selection=selectionForSheet(sheet);
  assert.deepEqual(selection,{sheet:'供应商报价',header_row:2,row:3,mapping:{supplier_id:'A',unit_price:'B'}});
  assert.deepEqual(mappingProblems(selection,sheet),[]);
  assert.deepEqual(previewBody(7,selection),{expected_revision:7,...selection});
});
test('unmapped freight and tax stay absent, never default to zero or included',()=>{
  assert.deepEqual(compactMapping({supplier_id:'A',shipping_cost:null,tax_mode:'',untrusted:'B'}),{supplier_id:'A'});
  assert.equal('shipping_cost' in previewBody(1,selectionForSheet(sheet)).mapping,false);
  assert.equal('tax_mode' in previewBody(1,selectionForSheet(sheet)).mapping,false);
});
test('mapping rejects wrong sheet, absent row, duplicate and absent columns',()=>{
  const selection=selectionForSheet(sheet);
  assert.ok(mappingProblems({...selection,sheet:'other'},sheet).length);
  assert.ok(mappingProblems({...selection,header_row:9},sheet).length);
  assert.ok(mappingProblems({...selection,row:2},sheet).length);
  assert.ok(mappingProblems({...selection,row:99},sheet).length);
  assert.ok(mappingProblems({...selection,mapping:{}},sheet).length);
  assert.ok(mappingProblems({...selection,mapping:{supplier_id:'A',sku:'A'}},sheet).length);
  assert.ok(mappingProblems({...selection,mapping:{unit_price:'Z'}},sheet).length);
});
test('selecting formula row never executes or replaces raw formula client-side',()=>{
  const selection={...selectionForSheet(sheet),row:4};
  assert.deepEqual(mappingProblems(selection,sheet),[]);
  assert.equal(sheet.rows[3].cells[1].value,'=1+1');
  assert.equal(sheet.rows[3].cells[1].formula,true);
  assert.equal(previewBody(2,selection).row,4);
});

test('archived, imported and unknown preview states remain read-only',()=>{
  assert.equal(importReadOnly('OPEN'),false);
  for(const status of ['ARCHIVED','IMPORTED','UNKNOWN',''])assert.equal(importReadOnly(status),true);
});

test('multi-item selection starts with one real data row without a scalar row key',()=>{
  const selection=selectionForSheet(sheet,true);
  assert.deepEqual(selection,{sheet:'供应商报价',header_row:2,rows:[3],mapping:{supplier_id:'A',unit_price:'B'}});
  assert.deepEqual(mappingProblems(selection,sheet),[]);
  assert.deepEqual(selectedRows(selection),[3]);
  assert.deepEqual(selectedRows(selectionForSheet(sheet)),[3]);
  assert.deepEqual(previewBody(9,selection),{expected_revision:9,...selection});
  assert.equal(Object.hasOwn(previewBody(9,selection),'row'),false);
  const noData=selectionForSheet({...sheet,rows:sheet.rows.slice(0,2)},true);
  assert.deepEqual(noData.rows,[]);
  assert.ok(mappingProblems(noData,sheet).some(problem=>problem.includes('1–20')));
});

test('multiple selected rows share the explicit mapping and preserve raw cell data',()=>{
  const selection={...selectionForSheet(sheet,true),rows:[3,4]};
  const original=structuredClone(sheet),body=previewBody(12,selection);
  assert.deepEqual(mappingProblems(selection,sheet),[]);
  assert.deepEqual(body,{expected_revision:12,sheet:'供应商报价',header_row:2,rows:[3,4],mapping:{supplier_id:'A',unit_price:'B'}});
  assert.notEqual(body.rows,selection.rows);
  assert.notEqual(body.mapping,selection.mapping);
  const chosen=selectedRows(selection);chosen.push(5);
  assert.deepEqual(selection.rows,[3,4]);
  assert.deepEqual(sheet,original);
  assert.equal(sheet.rows[3].cells[1].value,'=1+1');
  assert.equal('shipping_cost' in body.mapping,false);
  assert.equal('tax_mode' in body.mapping,false);
});

test('multi-row selection is limited to 1–20 distinct positive integer rows',()=>{
  const manyRows={...sheet,rows:[...sheet.rows,...Array.from({length:21},(_,index)=>({row:index+5,cells:[{column:'A',cell:`A${index+5}`,value:'SUP-A'}]}))]};
  const base=selectionForSheet(manyRows,true),twenty=Array.from({length:TABLE_MAX_ROWS},(_,index)=>index+3);
  assert.equal(TABLE_MAX_ROWS,20);
  assert.deepEqual(mappingProblems({...base,rows:twenty},manyRows),[]);
  assert.deepEqual(previewBody(1,{...base,rows:twenty}).rows,twenty);
  for(const rows of [[],[3,3],[3,4,4],[0],[-1],[3.5],['3'],[true],[null],[NaN],[Infinity],Array.from({length:21},(_,index)=>index+3)]){
    assert.ok(mappingProblems({...base,rows},manyRows).length,`invalid rows ${JSON.stringify(rows)}`);
    assert.throws(()=>previewBody(1,{...base,rows}),RangeError);
  }
});

test('row and rows are mutually exclusive and cannot be silently mixed or omitted',()=>{
  const selection=selectionForSheet(sheet,true);
  for(const value of [
    {...selection,row:3}, {...selection,row:undefined}, {...selection,rows:null}, {...selection,rows:3},
    {...selection,rows:'3'}, {sheet:sheet.name,header_row:2,mapping:selection.mapping},
    {...selectionForSheet(sheet),row:undefined}, {...selectionForSheet(sheet),row:null},
  ]){
    assert.ok(mappingProblems(value,sheet).length);
    assert.throws(()=>previewBody(1,value),RangeError);
  }
  assert.equal(Object.hasOwn(previewBody(1,selectionForSheet(sheet)),'rows'),false);
});

test('every multi-row coordinate must exist after the selected header on the selected sheet',()=>{
  const selection=selectionForSheet(sheet,true);
  for(const rows of [[2],[1,3],[3,99],[1,2]])assert.ok(mappingProblems({...selection,rows},sheet).length);
  assert.ok(mappingProblems({...selection,header_row:4},sheet).length);
  assert.ok(mappingProblems({...selection,sheet:'other'},sheet).length);
  assert.ok(mappingProblems(selection,undefined).length);
  assert.ok(mappingProblems({...selection,mapping:{sku:'A',unit_price:'A'}},sheet).length);
  assert.ok(mappingProblems({...selection,mapping:{unit_price:'Z'}},sheet).length);
});

test('multi-line preview renders quote headers once and uses each line evidence path',()=>{
  const values={supplier_id:'SUP-A',currency:'CNY',shipping_cost:'12.00',sku:'legacy-must-not-show',
    lines:[{sku:'A',quantity:'2',uom:'EA',unit_price:'0',tax_mode:'included',tax_rate:null,discount:'0',delivery_days:0},
      {sku:'B',quantity:'3',uom:'EA',unit_price:null,tax_mode:'unknown',tax_rate:null,discount:null,delivery_days:null}]};
  const snapshot=structuredClone(values),sections=previewSections(values);
  assert.equal(sections.length,3);
  assert.deepEqual(sections[0].fields.map(field=>field.path),['supplier_id','currency','shipping_cost']);
  assert.deepEqual(sections[1].fields.map(field=>field.path),TABLE_LINE_FIELDS.map(([field])=>`lines.0.${field}`));
  assert.deepEqual(sections[2].fields.map(field=>field.path),TABLE_LINE_FIELDS.map(([field])=>`lines.1.${field}`));
  const fields=sections.flatMap(section=>section.fields);
  for(const [key] of TABLE_HEADER_FIELDS)assert.equal(fields.filter(field=>field.field===key).length,1);
  assert.equal(fields.find(field=>field.path==='lines.0.unit_price').value,'0');
  assert.equal(fields.find(field=>field.path==='lines.0.delivery_days').value,0);
  assert.equal(fields.find(field=>field.path==='lines.1.unit_price').value,null);
  assert.equal(fields.find(field=>field.path==='lines.1.tax_mode').value,'unknown');
  assert.equal(fields.some(field=>field.value==='legacy-must-not-show'),false);
  assert.deepEqual(values,snapshot);
});

test('preview unknowns and missing fields remain unchanged in legacy and multi-item results',()=>{
  for(const lines of [undefined,null]){
    const sections=previewSections({supplier_id:'SUP-A',shipping_cost:null,tax_mode:'unknown',lines});
    assert.equal(sections.length,1);
    assert.equal(sections[0].label,null);
    assert.deepEqual(sections[0].fields.map(field=>field.path),TABLE_FIELDS.map(([field])=>field));
    assert.equal(sections[0].fields.find(field=>field.path==='shipping_cost').value,null);
    assert.equal(sections[0].fields.find(field=>field.path==='unit_price').value,undefined);
    assert.equal(sections[0].fields.find(field=>field.path==='tax_mode').value,'unknown');
  }
  const sections=previewSections({supplier_id:null,currency:null,shipping_cost:null,lines:[{tax_mode:'unknown'}]});
  assert.equal(sections[0].fields.find(field=>field.path==='shipping_cost').value,null);
  assert.equal(sections[1].fields.find(field=>field.path==='lines.0.unit_price').value,undefined);
  assert.equal(sections[1].fields.find(field=>field.path==='lines.0.tax_mode').value,'unknown');
});

test('repeated quote headers retain all supporting source rows without double-counting the first',()=>{
  const sources=[
    {kind:'source',sheet:'供应商报价',row:3,cell_range:'H3',fragment_id:'doc:r3:H',text:'Freight: 12.00'},
    {kind:'source',sheet:'供应商报价',row:4,cell_range:'H4',fragment_id:'doc:r4:H',text:'Freight: 12'},
  ];
  const combined={...sources[0],sources},snapshot=structuredClone(combined),visible=previewSources(combined);
  assert.deepEqual(visible,sources);
  assert.notEqual(visible,sources);
  assert.equal(visible.length,2);
  assert.deepEqual(visible.map(source=>source.cell_range),['H3','H4']);
  assert.deepEqual(visible.map(source=>source.text),['Freight: 12.00','Freight: 12']);
  visible.pop();assert.deepEqual(combined,snapshot);
});

test('legacy and per-line evidence stays intact, while absent evidence stays absent',()=>{
  const single={kind:'source',sheet:'供应商报价',row:4,cell_range:'B4',text:'Unit price: 0',fragment_id:'doc:r4:B'};
  assert.deepEqual(previewSources(single),[single]);
  assert.deepEqual(previewSources({...single,sources:[]}),[{...single,sources:[]}]);
  assert.deepEqual(previewSources(undefined),[]);
  assert.deepEqual(previewSources(null),[]);
  assert.deepEqual(previewSources({kind:'missing',reason:'unmapped'}),[{kind:'missing',reason:'unmapped'}]);
});
