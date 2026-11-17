import test from 'node:test';
import assert from 'node:assert/strict';
import {getEventListeners} from 'node:events';
import {ADVICE_READ_TIMEOUT_MS,ADVICE_WRITE_TIMEOUT_MS,AdviceWaitError,adviceFailureExplanation,providerAttemptExplanation,waitForAdvice} from '../lib/advice.mjs';

const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

test('advice read and write waits have finite separate bounds',()=>{
  assert.equal(ADVICE_READ_TIMEOUT_MS,15_000);assert.equal(ADVICE_WRITE_TIMEOUT_MS,60_000);
});
test('successful advice read returns unchanged receipt and releases timer/listeners',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const parent=new AbortController();let requestSignal;
  const receipt={status:'COMPLETED',output:{usage:null,usage_complete:false,cost:null}};
  assert.equal(await waitForAdvice(async signal=>{requestSignal=signal;return receipt;},{signal:parent.signal}),receipt);
  assert.equal(getEventListeners(parent.signal,'abort').length,0);
  assert.equal(getEventListeners(requestSignal,'abort').length,0);
  t.mock.timers.tick(ADVICE_READ_TIMEOUT_MS);
  assert.equal(requestSignal.aborted,false);
});
test('timeout releases a hung read even if its transport ignores abort; never retries',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const late=deferred();let calls=0,requestSignal;
  const pending=waitForAdvice(signal=>{calls++;requestSignal=signal;return late.promise;});
  const rejected=assert.rejects(pending,{code:'ADVICE_WAIT_TIMEOUT'});
  await Promise.resolve();t.mock.timers.tick(ADVICE_READ_TIMEOUT_MS);await rejected;
  assert.equal(requestSignal.aborted,true);assert.equal(calls,1);
  late.resolve({status:'COMPLETED'});await Promise.resolve();assert.equal(calls,1);
});
test('stop waiting fences a late POST response and does not replay it',async t=>{
  t.mock.timers.enable({apis:['setTimeout']});
  const parent=new AbortController(),late=deferred();let calls=0;
  const pending=waitForAdvice(()=>{calls++;return late.promise;},{signal:parent.signal,timeoutMs:ADVICE_WRITE_TIMEOUT_MS});
  const rejected=assert.rejects(pending,{code:'ADVICE_WAIT_CANCELLED'});
  await Promise.resolve();parent.abort(new AdviceWaitError('ADVICE_WAIT_CANCELLED'));await rejected;
  assert.equal(calls,1);assert.equal(getEventListeners(parent.signal,'abort').length,0);
  // A transport can reject after abandonment; its result remains consumed.
  late.reject(new Error('Late transport failure'));await Promise.resolve();
  t.mock.timers.tick(ADVICE_WRITE_TIMEOUT_MS);assert.equal(calls,1);
});
test('abandoned request/identity or changed source cannot dispatch new work',async()=>{
  for(const code of ['ADVICE_WAIT_CANCELLED','ADVICE_CONTEXT_CHANGED']) {
    const parent=new AbortController();parent.abort(new AdviceWaitError(code));
    await assert.rejects(waitForAdvice(()=>assert.fail('Abandoned scope dispatched'),{signal:parent.signal}),{code});
  }
});
test('same-turn cancellation before dispatch avoids any request',async()=>{
  const parent=new AbortController();
  const pending=waitForAdvice(()=>assert.fail('Cancelled operation dispatched'),{signal:parent.signal});
  parent.abort(new AdviceWaitError('ADVICE_WAIT_CANCELLED'));
  await assert.rejects(pending,{code:'ADVICE_WAIT_CANCELLED'});
});
test('transport error stays intact and is not retried',async()=>{
  let calls=0;const error=new Error('Synthetic HTTP failure');
  await assert.rejects(waitForAdvice(async()=>{calls++;throw error;}),value=>value===error);
  assert.equal(calls,1);
});
test('invalid timeout cannot silently create an unbounded wait',async()=>{
  for(const timeoutMs of [0,-1,NaN,Infinity,1.5])
    await assert.rejects(waitForAdvice(()=>assert.fail('Invalid operation dispatched'),{timeoutMs}),RangeError);
});
test('failure explanations distinguish refusal, empty output and uncertain fees',()=>{
  assert.match(adviceFailureExplanation('MODEL_REFUSED'),/拒绝/);
  assert.match(adviceFailureExplanation('MODEL_EMPTY_OUTPUT'),/未返回可用内容/);
  for(const code of ['MODEL_TIMEOUT','MODEL_CANCELLED','MODEL_CALL_FAILED'])
    assert.match(adviceFailureExplanation(code),/费用/);
  assert.match(adviceFailureExplanation('MODEL_CANCELLED'),/无法据此确认/);
  assert.match(adviceFailureExplanation('MODEL_OUTPUT_INCOMPLETE'),/不完整/);
  assert.match(adviceFailureExplanation('UNKNOWN_CODE'),/核对已保存的运行状态/);
});
test('provider attempts stay distinct from model rounds and preserve legacy unknown values',()=>{
  assert.equal(providerAttemptExplanation({model_calls:2,provider_attempts:3}),'提供方请求尝试 3 次');
  assert.equal(providerAttemptExplanation({model_calls:2}),'提供方请求尝试次数未报告');
  assert.equal(providerAttemptExplanation({provider_attempts:0}),'提供方请求尝试 0 次');
  for(const provider_attempts of [null,undefined,NaN,-1,1.2,'3'])
    assert.equal(providerAttemptExplanation({provider_attempts}),'提供方请求尝试次数未报告');
});
