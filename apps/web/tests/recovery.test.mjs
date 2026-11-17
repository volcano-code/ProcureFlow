import test from 'node:test';
import assert from 'node:assert/strict';
import {RecoveryReader,assertReconciliationBinding,reconciliationMessage} from '../lib/recovery.mjs';
import {SessionScope} from '../lib/session.mjs';
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const detail={id:'op1',ledger_sha256:'ledger-1'};
const receipt=(extra={})=>({operation_id:'op1',ledger_sha256:'ledger-1',status:'verified',simulated:true,network_attempted:false,
  external_write_attempted:false,replay_permitted:false,matches_snapshot:true,draft_verified:true,
  expected:{docstatus:0},observed:{docstatus:0},differences:[],...extra});

test('all diagnostics are explicit GETs through credential-safe requestJSON; IDs are encoded',async()=>{
  const calls=[],session=new SessionScope('synthetic');
  const reader=new RecoveryReader('http://localhost',session,{fetchImpl:async(url,options)=>{
    calls.push(url);assert.equal(options.method,'GET');assert.equal(options.body,undefined);
    assert.equal(options.headers.Authorization,'Bearer synthetic');assert.equal(options.credentials,'omit');
    assert.equal(options.cache,'no-store');assert.equal(options.redirect,'error');return Response.json({});
  }});
  await reader.read('list','op/&?').promise;await reader.read('detail','op/&?').promise;
  await reader.read('reconciliation','op/&?').promise;
  assert.deepEqual(calls,['http://localhost/api/v1/recovery/operations?limit=50&after=op%2F%26%3F',
    'http://localhost/api/v1/recovery/operations/op%2F%26%3F','http://localhost/api/v1/recovery/operations/op%2F%26%3F/reconciliation']);
  reader.close();session.invalidate();
});
test('repeated same-target clicks share one in-flight request; deliberate later read is fresh',async()=>{
  const pending=deferred();let calls=0;
  const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>{calls++;return calls===1?pending.promise:Promise.resolve(Response.json({new:true}));}});
  const first=reader.read('reconciliation','op1'),second=reader.read('reconciliation','op1');
  assert.equal(first,second);assert.equal(calls,1);pending.resolve(Response.json({new:false}));
  assert.deepEqual(await first.promise,{new:false});assert.deepEqual(await reader.read('reconciliation','op1').promise,{new:true});
  assert.equal(calls,2);reader.close();
});
test('new operation selection rejects a late detail response even when fetch ignores abort',async()=>{
  const pending=deferred();let calls=0;
  const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>++calls===1?pending.promise:Promise.resolve(Response.json({id:'op2'}))});
  const old=reader.read('detail','op1'),rejected=assert.rejects(old.promise,{name:'AbortError'});
  const next=reader.read('detail','op2');assert.equal(old.signal.aborted,true);
  pending.resolve(Response.json({id:'op1'}));await rejected;assert.deepEqual(await next.promise,{id:'op2'});reader.close();
});
test('pagination rejects a slower previous page instead of mixing tenants or pages',async()=>{
  const pending=deferred();let calls=0;
  const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>++calls===1?pending.promise:Promise.resolve(Response.json({items:['page2']}))});
  const old=reader.read('list'),rejected=assert.rejects(old.promise,{name:'AbortError'});
  const next=reader.read('list','cursor');pending.resolve(Response.json({items:['page1']}));
  await rejected;assert.deepEqual(await next.promise,{items:['page2']});reader.close();
});
test('closing details cancels the readback without retrying or disturbing unrelated list reads',async()=>{
  const pending=deferred();let calls=0;
  const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:url=>{calls++;return url.endsWith('/reconciliation')?pending.promise:Promise.resolve(Response.json({items:[]}));}});
  const list=reader.read('list');await list.promise;
  const check=reader.read('reconciliation','op1'),rejected=assert.rejects(check.promise,{name:'AbortError'});
  reader.cancel('reconciliation');assert.equal(list.signal.aborted,false);
  pending.resolve(Response.json(receipt()));await rejected;assert.equal(calls,2);reader.close();
});
test('unmount closes all diagnostic lanes and refuses future reads',async()=>{
  const waits=[],reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>{const item=deferred();waits.push(item);return item.promise;}});
  const tasks=[reader.read('list'),reader.read('detail','op1'),reader.read('reconciliation','op1')];
  const rejected=tasks.map(task=>assert.rejects(task.promise,{name:'AbortError'}));reader.close();
  assert.ok(tasks.every(task=>task.signal.aborted));waits.forEach(item=>item.resolve(Response.json({})));
  await Promise.all(rejected);assert.throws(()=>reader.read('list'),{name:'AbortError'});
});
test('session replacement while JSON is parsing cannot reveal a previous identity result',async()=>{
  const parsing=deferred(),started=deferred(),session=new SessionScope('old');
  const reader=new RecoveryReader('http://localhost',session,{fetchImpl:async()=>({ok:true,status:200,json(){started.resolve();return parsing.promise;}})});
  const task=reader.read('detail','op1'),rejected=assert.rejects(task.promise,{name:'AbortError'});
  await started.promise;session.invalidate('replaced');parsing.resolve({id:'old-tenant-operation'});
  await rejected;reader.close();
});
test('401 clears authority; a role denial preserves session; neither retries',async()=>{
  for(const status of [401,403]){
    let calls=0;const session=new SessionScope('synthetic');
    const reader=new RecoveryReader('http://localhost',session,{fetchImpl:async()=>{calls++;return Response.json({error:{code:'DENIED',message:'Denied'}},{status});}});
    await assert.rejects(reader.read('list').promise,error=>error.status===status);
    assert.equal(session.active,status===403);assert.equal(calls,1);reader.close();session.invalidate();
  }
});
test('network and malformed response failures remain visible and never trigger automatic retry',async()=>{
  for(const response of [()=>Promise.reject(new TypeError('network lost')),()=>Promise.resolve(new Response('not JSON'))]){
    let calls=0;const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>{calls++;return response();}});
    await assert.rejects(reader.read('reconciliation','op1').promise);await new Promise(resolve=>setTimeout(resolve,5));
    assert.equal(calls,1);reader.close();
  }
});
test('a receipt must match both the selected operation and exact displayed ledger',()=>{
  assert.equal(assertReconciliationBinding(receipt(),detail).status,'verified');
  for(const extra of [{operation_id:'op2'},{ledger_sha256:'changed'}])
    assert.throws(()=>assertReconciliationBinding(receipt(extra),detail),error=>error.code==='RECOVERY_BINDING_CHANGED');
  assert.throws(()=>assertReconciliationBinding(receipt(),{...detail,ledger_sha256:''}));
});
test('unknown status, forbidden write/replay claims, and contradictory verified results fail closed',()=>{
  for(const extra of [{status:'success'},{external_write_attempted:true},{replay_permitted:true},{simulated:undefined},
    {network_attempted:undefined},{matches_snapshot:false},{draft_verified:false},{observed:null},{observed:undefined},
    {expected:null},{differences:[{field:'quantity'}]}])
    assert.throws(()=>assertReconciliationBinding(receipt(extra),detail),error=>error.code==='INVALID_RECOVERY_RESPONSE');
});
test('missing, mismatch, blocked and unavailable preserve uncertainty and never grant replay',()=>{
  for(const status of ['missing','mismatch','blocked','unavailable']){
    assert.equal(assertReconciliationBinding(receipt({status,matches_snapshot:false,draft_verified:false,observed:null}),detail).replay_permitted,false);
    assert.match(reconciliationMessage(status),/禁止重放/);
  }
  assert.match(reconciliationMessage('missing'),/查无记录不能证明未提交/);
  assert.match(reconciliationMessage('verified'),/不授予重放权限/);
  assert.match(reconciliationMessage('unexpected'),/结果仍不确定/);
});
test('reader rejects unsupported actions and empty operation IDs before network access',()=>{
  let calls=0;const reader=new RecoveryReader('http://localhost','synthetic',{fetchImpl:()=>calls++});
  assert.throws(()=>reader.read('release','op1'));assert.throws(()=>reader.read('detail',''));
  assert.throws(()=>reader.read('reconciliation',null));assert.equal(calls,0);reader.close();
});
