import test from 'node:test';
import assert from 'node:assert/strict';
import {AGUIDecoder, observeAdvice} from '../lib/agui.mjs';
import {SessionScope} from '../lib/session.mjs';
const enc = new TextEncoder();
const ev = (data,id)=> (id?`id: ${id}\n`:``)+`data: ${JSON.stringify(data)}\n\n`;
const start={type:'RUN_STARTED',threadId:'req_1',runId:'adv_1',protocolVersion:'1.0'};
const state={type:'STATE_SNAPSHOT',snapshot:{requestId:'req_1',adviceRunId:'adv_1',agentStatus:'RUNNING',
  current:true,humanAction:'observe_only',sourceReadOnly:true,externalWritePermitted:false,auditCursor:9}};
const progress={type:'CUSTOM',name:'procureflow.advice.progress',value:{auditId:9,kind:'AGENT_TOOL_STARTED',detail:{tool:'search_policy'}}};
const finish={type:'RUN_FINISHED',threadId:'req_1',runId:'adv_1',result:{mode:'observation_only',agentStatus:'RUNNING',providerSuccessNotImplied:true}};
function response(payload,status=200){return {ok:status===200,status,headers:new Headers({'content-type':'text/event-stream'}),
  body:new ReadableStream({start(controller){controller.enqueue(enc.encode(payload));controller.close();}})};}
const wire=ev(start)+ev(state)+ev(progress,9)+ev(finish);
test('AG-UI decoder supports split UTF-8 chunks and stable replay cursor',()=>{
 const d=new AGUIDecoder('req_1','adv_1');const bytes=enc.encode(wire);const got=[];
 for(let i=0;i<bytes.length;i+=3) got.push(...d.feed(bytes.slice(i,i+3)));
 d.done();assert.deepEqual(got.map(x=>x.type),['RUN_STARTED','STATE_SNAPSHOT','CUSTOM','RUN_FINISHED']);assert.equal(d.cursor,9);
});
test('rejects forged scope, stale framing, unfinished streams and fake audit IDs',()=>{
 for(const value of [ev({...start,runId:'adv_other'})+ev(state)+ev(finish),ev(state)+ev(finish),
    ev(start)+ev(state)+ev({...progress,value:{...progress.value,auditId:5}},9)+ev(finish)]) {
   const d=new AGUIDecoder('req_1','adv_1');assert.throws(()=>{d.feed(enc.encode(value));d.done();},{name:'AGUIError'});
 }
 const d=new AGUIDecoder('req_1','adv_1');d.feed(enc.encode(ev(start)+ev(state)));
 assert.throws(()=>d.done(),{code:'AGUI_TRUNCATED'});
});
test('observes only persisted receipt through one POST, never dispatches work',async()=>{
 const calls=[];const seen=[];
 const fetchImpl=async(url,opts)=>{calls.push({url,opts});return response(wire);};
 const r=await observeAdvice('http://localhost:3000',new SessionScope('secret'),{
   runId:'adv_1',threadId:'req_1',after:2,fetchImpl,onEvent:e=>seen.push(e)});
 assert.equal(calls.length,1);assert.match(calls[0].url,/ag-ui\/advice-runs\/adv_1\/observe\?after=2$/);
 assert.equal(calls[0].opts.method,'POST');assert.equal(calls[0].opts.redirect,'error');
 assert.deepEqual(JSON.parse(calls[0].opts.body),{threadId:'req_1',runId:'adv_1',protocolVersion:'1.0',messages:[],tools:[],context:[],state:{}});
 assert.equal(r.cursor,9);assert.equal(r.state.agentStatus,'RUNNING');
 assert.deepEqual(seen.map(x=>x.type),['RUN_STARTED','STATE_SNAPSHOT','CUSTOM','RUN_FINISHED']);
});
test('invalid identity response revokes the scoped session and no stream is consumed',async()=>{
 let revoked=0;const credential=new SessionScope('secret',{onInvalidated:()=>revoked++});
 await assert.rejects(observeAdvice('http://localhost',credential,{runId:'adv_1',threadId:'req_1',
   fetchImpl:async()=>({ok:false,status:401})}),{code:'AGUI_DENIED'});
 assert.equal(revoked,1);assert.equal(credential.active,false);
});
test('disconnected stream cannot be treated as completed',async()=>{
 await assert.rejects(observeAdvice('http://localhost','fake',{runId:'adv_1',threadId:'req_1',
   fetchImpl:async()=>response(ev(start)+ev(state))}),{code:'AGUI_TRUNCATED'});
});
test('arbitrary upstream event, raw reasoning or mutable AG-UI payload cannot reach UI',()=>{
 for(const type of ['REASONING_MESSAGE_CONTENT','RUN_ERROR','TOOL_CALL_ARGS']){
  const d=new AGUIDecoder('req_1','adv_1');
  assert.throws(()=>d.feed(enc.encode(ev(start)+ev(state)+ev({type,delta:'secret'})+ev(finish))),{code:'AGUI_UNSUPPORTED_EVENT'});
 }
});
