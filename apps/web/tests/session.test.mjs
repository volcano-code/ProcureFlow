import test from 'node:test';
import assert from 'node:assert/strict';
import {SessionScope} from '../lib/session.mjs';
import {requestJSON,watchAudit,loadDemo,AuditDecoder} from '../lib/transport.mjs';
const bytes=text=>new TextEncoder().encode(text);
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};

test('session secrets never serialize or become component identity keys',()=>{
  const session=new SessionScope('private-secret');
  assert.ok(!JSON.stringify(session).includes('private-secret'));
  assert.ok(!String(session).includes('private-secret'));
  session.invalidate('logout');
  assert.throws(()=>session.authorization(),{name:'AbortError'});
});
test('logout immediately aborts all child requests and fences ignored abort responses',async()=>{
  const session=new SessionScope('synthetic'),pending=deferred();let signal;
  const request=requestJSON('http://localhost',session,'/requests',{fetchImpl:(_,options)=>{signal=options.signal;return pending.promise;}});
  session.invalidate('logout');assert.equal(signal.aborted,true);
  pending.resolve(Response.json([{id:'old-tenant'}]));
  await assert.rejects(request,{name:'AbortError'});
});
test('auth failure anywhere invalidates entire session even with malformed denial body',async()=>{
  const reasons=[],session=new SessionScope('synthetic',{onInvalidated:reason=>reasons.push(reason)});
  await assert.rejects(()=>requestJSON('http://localhost',session,'/policy',{fetchImpl:async()=>new Response('not JSON',{status:401})}));
  assert.equal(session.active,false);assert.deepEqual(reasons,['invalid']);
  let requests=0;
  await assert.rejects(()=>requestJSON('http://localhost',session,'/requests',{method:'POST',fetchImpl:async()=>{requests++;return Response.json({});}}),{name:'AbortError'});
  assert.equal(requests,0);
});
test('role authorization failure does not discard a valid session',async()=>{
  const session=new SessionScope('synthetic');
  await assert.rejects(()=>requestJSON('http://localhost',session,'/policy/versions',{method:'POST',fetchImpl:async()=>Response.json({error:{code:'FORBIDDEN',message:'Role denied'}},{status:403})}),error=>error.status===403);
  assert.equal(session.active,true);session.invalidate();
});
test('JSON parsing race cannot resurrect an invalidated session',async()=>{
  const session=new SessionScope('synthetic'),payload=deferred(),started=deferred();
  const request=requestJSON('http://localhost',session,'/me',{fetchImpl:async()=>({ok:true,status:200,json(){started.resolve();return payload.promise;}})});
  await started.promise;session.invalidate('replaced');payload.resolve({user_id:'old'});
  await assert.rejects(request,{name:'AbortError'});
});
test('new login using same static credential is isolated from previous generation',async()=>{
  const old=new SessionScope('same'),next=new SessionScope('same');old.invalidate('replaced');
  assert.notEqual(old.id,next.id);
  let calls=0;
  await assert.rejects(()=>requestJSON('http://localhost',old,'/requests',{method:'POST',fetchImpl:async()=>{calls++;return Response.json({});}}));
  assert.deepEqual(await requestJSON('http://localhost',next,'/me',{fetchImpl:async()=>{calls++;return Response.json({user_id:'next'});}}),{user_id:'next'});
  assert.equal(calls,1);next.invalidate();
});
test('expiry is automatic and exact-boundary requests fail closed',async()=>{
  const reasons=[],session=new SessionScope('expiring',{expiresAt:new Date(Date.now()+25).toISOString(),onInvalidated:r=>reasons.push(r)});
  await new Promise(resolve=>setTimeout(resolve,45));assert.equal(session.active,false);assert.deepEqual(reasons,['expired']);
  assert.throws(()=>session.authorization(),{name:'AbortError'});
  assert.throws(()=>new SessionScope('t',{expiresAt:'invalid'}),/Invalid session expiry/);
});
test('terminal auth_invalid SSE frame invalidates without audit identity or retry',async()=>{
  const session=new SessionScope('synthetic'),events=[],statuses=[];let calls=0;
  await watchAudit('http://localhost',session,'r1',{signal:new AbortController().signal,
    fetchImpl:async()=>{calls++;return new Response(bytes('data: {"id":1,"type":"A"}\n\nevent: auth_invalid\ndata: {"code":"UNAUTHENTICATED"}\n\ndata: {"id":2,"type":"B"}\n\n'));},
    onEvent:event=>events.push(event.id),onStatus:value=>statuses.push(value)});
  assert.deepEqual(events,[1]);assert.equal(calls,1);assert.equal(session.active,false);assert.equal(statuses.at(-1),'denied');
});
test('SSE HTTP 401 invalidates globally while 403 only denies that stream',async()=>{
  for(const status of [401,403]){
    const session=new SessionScope('synthetic');let calls=0;
    await watchAudit('http://localhost',session,'r',{signal:new AbortController().signal,onEvent:()=>assert.fail(),fetchImpl:async()=>{calls++;return new Response('',{status});}});
    assert.equal(session.active,status===403);assert.equal(calls,1);session.invalidate();
  }
});
test('independent session context survives another context invalidation',async()=>{
  const a=new SessionScope('a'),b=new SessionScope('b');a.invalidate('logout');
  const data=await requestJSON('http://localhost',b,'/me',{fetchImpl:async(_,options)=>{assert.equal(options.headers.Authorization,'Bearer b');assert.equal(options.credentials,'omit');return Response.json({user_id:'b'});}});
  assert.equal(data.user_id,'b');assert.equal(b.active,true);b.invalidate();
});
test('unauthenticated invitation exchange uses POST body only and omits ambient cookies',async()=>{
  await requestJSON('http://localhost','', '/auth/login',{method:'POST',body:{credential:'one-use'},fetchImpl:async(url,options)=>{
    assert.equal(url,'http://localhost/api/v1/auth/login');assert.equal(options.headers.Authorization,undefined);
    assert.equal(options.credentials,'omit');assert.equal(options.cache,'no-store');assert.deepEqual(JSON.parse(options.body),{credential:'one-use'});
    return Response.json({});
  }});
});
test('cancelling demo import fences follow-on upload mutation and retained callback',async()=>{
  const session=new SessionScope('synthetic'),pending=deferred();let calls=0,created=0;
  const result=loadDemo('http://localhost',session,{onCreated:()=>created++,fetchImpl:async()=>{
    calls++;if(calls===1)return Response.json({synthetic:true,files:['a','b','c'],request:{}});
    session.invalidate('logout');return pending.promise;
  }});
  pending.resolve(Response.json({id:'old'}));await assert.rejects(result,{name:'AbortError'});
  assert.equal(calls,2);assert.equal(created,0);
});

test('late denial from a replaced session cannot invalidate the new identity',async()=>{
  const reasons=[],old=new SessionScope('same'),next=new SessionScope('same',{onInvalidated:r=>reasons.push(r)}),pending=deferred();
  const response=requestJSON('http://localhost',old,'/policy',{fetchImpl:()=>pending.promise});
  old.invalidate('replaced');pending.resolve(Response.json({error:{code:'UNAUTHENTICATED',message:'Expired'}},{status:401}));
  await assert.rejects(response,{name:'AbortError'});
  assert.equal(next.active,true);assert.deepEqual(reasons,[]);next.invalidate();
});

test('caller cancellation fences late data without invalidating an otherwise valid session',async()=>{
  const session=new SessionScope('active'),local=new AbortController(),pending=deferred();
  const response=requestJSON('http://localhost',session,'/documents/old/evidence',{signal:local.signal,fetchImpl:()=>pending.promise});
  local.abort();pending.resolve(Response.json({text:'stale evidence'}));
  await assert.rejects(response,{name:'AbortError'});assert.equal(session.active,true);session.invalidate();
});
