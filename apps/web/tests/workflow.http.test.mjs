/** Actual fetch/FormData through the same transport imported by Next.js.
 * This is NOT a browser/React/Next-build test.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {requestJSON,loadDemo,watchAudit} from '../lib/transport.mjs';
import {previewBody,selectionForSheet} from '../lib/table-import.mjs';
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

test('native frontend transport: ordinary CSV preview -> explicit map -> unconfirmed quote -> approved draft',async()=>{
  const r=await call('demo-buyer','/requests','POST',{title:'Synthetic table HTTP flow',sku:'STAND-01',quantity:'20',budget:'30000.00',max_delivery_days:14});
  const form=new FormData();form.append('file',new Blob(['supplier_id,sku,quantity,uom,unit_price,tax_mode,tax_rate,shipping_cost,discount,delivery_days,currency\nSUP-A,STAND-01,20,EA,1200.00,included,0.13,800.00,0.00,7,CNY\n'],{type:'text/csv'}),'synthetic-table.csv');
  const draft=await call('demo-buyer',`/requests/${r.id}/table-imports`,'POST',form);
  assert.equal(draft.status,'OPEN');assert.equal(draft.values,null);
  assert.deepEqual(await call('demo-buyer',`/requests/${r.id}/quotes`),[]);
  const selection=selectionForSheet(draft.sheets[0]);
  const mapped=await call('demo-buyer',`/table-imports/${draft.id}/preview`,'POST',previewBody(draft.revision,selection));
  assert.equal(mapped.evidence.unit_price.cell_range,'E2');assert.equal(mapped.evidence.unit_price.document_sha256,draft.document_sha256);
  const command={expected_revision:mapped.revision,acknowledge:true};
  const q=await call('demo-buyer',`/table-imports/${draft.id}/confirm`,'POST',command);
  assert.equal(q.confirmed_by,null);assert.equal(q.calculation.eligible,false);
  const replay=await call('demo-buyer',`/table-imports/${draft.id}/confirm`,'POST',command);
  assert.equal(replay.id,q.id);assert.equal((await call('demo-buyer',`/requests/${r.id}/quotes`)).length,1);
  await call('demo-buyer',`/quotes/${q.id}/confirm`,'POST',{expected_version:q.version,acknowledge:true});
  const analysis=await call('demo-buyer',`/requests/${r.id}/analyze`,'POST',{});
  assert.equal(analysis.proposal.total,'24800.00');
  const body={snapshot_hash:analysis.proposal.snapshot_hash};
  await call('demo-approver',`/requests/${r.id}/approval`,'POST',body);
  const op=await call('demo-buyer',`/requests/${r.id}/execute`,'POST',body);
  const done=await call('demo-buyer',`/operations/${op.id}/process`,'POST');
  assert.equal(done.status,'COMPLETED');assert.ok(done.remote_id.startsWith('MOCK-SQ-'));
  const receipt=await call('demo-buyer',`/table-imports/${draft.id}`);assert.equal(receipt.status,'IMPORTED');assert.equal(receipt.quote_id,q.id);
});

test('native frontend transport: tenant policy publication invalidates bound approval and preserves history',async()=>{
  const p=await prepare(),body={snapshot_hash:p.snapshot_hash};
  await call('demo-approver',`/requests/${p.id}/approval`,'POST',body);
  const original=await call('demo-buyer','/policy');
  const other=await call('demo-other-tenant','/policy');
  const command={expected_version:original.latest_version,budget_cap:'24000.00',max_delivery_days:14,
    minimum_valid_quotes:2,effective_at:null,reason:'Disposable HTTP policy regression'};
  await assert.rejects(()=>call('demo-buyer','/policy/versions','POST',command),e=>e.status===403);
  const created=await call('demo-approver','/policy/versions','POST',command);
  assert.equal(created.version,original.latest_version+1);
  assert.equal(created.status,'effective');
  await assert.rejects(()=>call('demo-approver','/policy/versions','POST',command),e=>e.status===409);
  assert.deepEqual(await call('demo-other-tenant','/policy'),other);
  const current=await call('demo-buyer',`/requests/${p.id}`);
  assert.equal(current.status,'APPROVAL_STALE');assert.equal(current.proposal_current,false);
  await assert.rejects(()=>call('demo-buyer',`/requests/${p.id}/execute`,'POST',body),e=>e.status===409);
  const approvals=await call('demo-auditor',`/requests/${p.id}/approvals`);
  assert.equal(approvals[0].snapshot_hash,p.snapshot_hash);assert.equal(approvals[0].status,'STALE');
  const history=await call('demo-auditor',`/requests/${p.id}/evaluations`);
  assert.equal(history[0].policy_hash,original.policy_hash);assert.equal(history[0].current,false);
  assert.equal(history[0].result.proposal.snapshot_hash,p.snapshot_hash);
  const blocked=await call('demo-buyer',`/requests/${p.id}/analyze`,'POST',{});
  assert.equal(blocked.proposal,null);assert.ok(blocked.violations.includes('INSUFFICIENT_VALID_QUOTES'));
  assert.equal(blocked.policy.policy_hash,created.policy_hash);
  assert.ok(blocked.quotes.some(q=>q.calculation.violations.includes('BUDGET_EXCEEDED')));
  const versions=await call('demo-auditor','/policy/versions');
  assert.equal(versions[0].id,created.id);assert.equal(versions[1].policy_hash,original.policy_hash);
  await assert.rejects(()=>call('demo-other-tenant',`/requests/${p.id}/evaluations`),e=>e.status===404);
});
