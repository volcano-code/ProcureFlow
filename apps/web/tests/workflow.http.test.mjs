/** Actual fetch/FormData through the same transport imported by Next.js.
 * This is NOT a browser/React/Next-build test.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {requestJSON,loadDemo,watchAudit} from '../lib/transport.mjs';
const base=process.env.PF_TEST_API_URL;
if(!base || !['127.0.0.1','localhost'].includes(new URL(base).hostname) || process.env.PF_ALLOW_TEST_MUTATIONS!=='1')
  throw new Error('Use scripts/web_http_smoke.py; requires an explicitly disposable local mock API');
const health=await (await fetch(base+'/health')).json();
assert.equal(health.mode,'demo');assert.equal(health.erp,'mock');
const call=(token,path,method='GET',body)=>requestJSON(base,token,path,{method,body});
async function prepare(){
  const id=await loadDemo(base,'demo-buyer');
  let quotes=await call('demo-buyer',`/requests/${id}/quotes`);
  assert.equal(quotes.length,3);assert.ok(quotes.every(q=>q.confirmed_by===null));
  assert.equal(quotes.find(q=>q.values.supplier_id==='SUP-B').calculation.total,null);
  for(const q of quotes)await call('demo-buyer',`/quotes/${q.id}/confirm`,'POST',{expected_version:q.version,acknowledge:true});
  const analysis=await call('demo-buyer',`/requests/${id}/analyze`,'POST',{});
  assert.equal(analysis.proposal.total,'24200.00');assert.equal(analysis.proposal.quote_values.supplier_id,'SUP-C');
  return {id,snapshot_hash:analysis.proposal.snapshot_hash};
}
test('native frontend transport: evidence -> independent approval -> idempotent mock draft',async()=>{
  const p=await prepare(),body={snapshot_hash:p.snapshot_hash};
  await assert.rejects(()=>call('demo-buyer',`/requests/${p.id}/approval`,'POST',body),e=>e.status===403);
  await call('demo-approver',`/requests/${p.id}/approval`,'POST',body);
  const op=await call('demo-buyer',`/requests/${p.id}/execute`,'POST',body);
  const done=await call('demo-buyer',`/operations/${op.id}/process`,'POST');
  assert.equal(done.status,'COMPLETED');assert.ok(done.remote_id.startsWith('MOCK-SQ-'));
  const replay=await call('demo-buyer',`/requests/${p.id}/execute`,'POST',body);
  assert.equal(replay.id,op.id);assert.equal(replay.remote_id,done.remote_id);
  const quotes=await call('demo-buyer',`/requests/${p.id}/quotes`),q=quotes.find(x=>x.values.supplier_id==='SUP-C');
  const evidence=await call('demo-buyer',`/documents/${q.document_id}/evidence`);
  assert.equal(evidence.sha256,q.document_sha256);
  await assert.rejects(()=>call('demo-other-tenant',`/requests/${p.id}`),e=>e.status===404);
});
test('native frontend transport: request modification invalidates approved snapshot',async()=>{
  const p=await prepare(),body={snapshot_hash:p.snapshot_hash};
  await call('demo-approver',`/requests/${p.id}/approval`,'POST',body);
  const r=await call('demo-buyer',`/requests/${p.id}`);
  const updated=await call('demo-buyer',`/requests/${p.id}`,'PUT',{expected_version:r.version,title:r.title,sku:r.sku,
    quantity:'21',budget:r.budget,max_delivery_days:r.max_delivery_days,uom:r.uom,currency:r.currency});
  assert.equal(updated.status,'APPROVAL_STALE');assert.equal(updated.proposal,null);
  await assert.rejects(()=>call('demo-buyer',`/requests/${p.id}/execute`,'POST',body),e=>e.status===409);
});
test('native frontend transport: authenticated real SSE can be read and cancelled',async()=>{
  const p=await prepare(),abort=new AbortController(),received=[];
  const timer=setTimeout(()=>abort.abort(),5000);
  try{await watchAudit(base,'demo-buyer',p.id,{signal:abort.signal,onEvent:e=>{received.push(e);abort.abort();}});}
  finally{clearTimeout(timer);}
  assert.ok(received.length>0);assert.equal(received[0].type,'REQUEST_CREATED');
});
