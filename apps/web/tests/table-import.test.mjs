import test from 'node:test';
import assert from 'node:assert/strict';
import {createImportScope,importExpired,compactMapping,mappingProblems,selectionForSheet,previewBody} from '../lib/table-import.mjs';
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
