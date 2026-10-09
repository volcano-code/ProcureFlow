import test from 'node:test';
import assert from 'node:assert/strict';
import {strictestBudget,bindingCurrent,staleExplanation,violationExplanation} from '../lib/policy.mjs';
import {requestJSON,APIError} from '../lib/transport.mjs';

test('nullable policy cap preserves the request constraint',()=>{
  assert.equal(strictestBudget('30000.00',null),'30000.00');
});
test('stricter cap is selected in either direction without rewriting decimals',()=>{
  assert.equal(strictestBudget('30000.00','25000.10'),'25000.10');
  assert.equal(strictestBudget('30000.00','35000'),'30000.00');
  assert.equal(strictestBudget('30.0','30.00'),'30.0');
  assert.equal(strictestBudget('0.11','0.1'),'0.1');
  assert.equal(strictestBudget('3E+4','25000.00'),'25000.00');
  assert.equal(strictestBudget('30000.00','2.5E+4'),'2.5E+4');
  assert.equal(strictestBudget('0E-999999','0.01'),'0E-999999');
  assert.equal(strictestBudget('1.01E+3','1010.01'),'1.01E+3');
});
test('budget display comparisons do not lose precision through Number',()=>{
  assert.equal(strictestBudget('9007199254740993.01','9007199254740993.00'),'9007199254740993.00');
  assert.equal(strictestBudget('9007199254740993.00','9007199254740993.01'),'9007199254740993.00');
});
test('a changed policy binding can never be described as current',()=>{
  assert.equal(bindingCurrent({current:true,policy_hash:'old'},'new'),false);
  assert.equal(bindingCurrent({current:true,policy_hash:'same'},'same'),true);
  assert.equal(bindingCurrent({current:false,policy_hash:'same'},'same'),false);
  assert.equal(bindingCurrent(null,'same'),false);
  assert.equal(bindingCurrent({current:true},'same'),false);
  assert.equal(bindingCurrent({current:true,policy_hash:'same'},null),false);
});
test('violation explanations retain missing-value and strictest-cap meaning',()=>{
  assert.match(violationExplanation('BUDGET_EXCEEDED'),/较严格/);
  assert.match(violationExplanation('DELIVERY_EXCEEDS_LIMIT'),/较严格/);
  assert.match(violationExplanation('UNKNOWN_SHIPPING_COST'),/不能自动推测为零/);
  assert.match(violationExplanation('INSUFFICIENT_VALID_QUOTES'),/不能生成可审批方案/);
  assert.match(staleExplanation('POLICY_CHANGED'),/重新校验/);
});
test('policy publication carries latest version and decimal strings once; conflict is surfaced',async()=>{
  let calls=0;
  await assert.rejects(()=>requestJSON('http://localhost:8000','approver','/policy/versions',{
    method:'POST',body:{expected_version:4,budget_cap:'24000.10',max_delivery_days:null,minimum_valid_quotes:2,
      effective_at:null,reason:'Confirmed procurement policy change'},fetchImpl:async(url,options)=>{
      calls++;assert.equal(url,'http://localhost:8000/api/v1/policy/versions');
      assert.equal(options.method,'POST');assert.equal(options.headers.Authorization,'Bearer approver');
      assert.deepEqual(JSON.parse(options.body),{expected_version:4,budget_cap:'24000.10',max_delivery_days:null,
        minimum_valid_quotes:2,effective_at:null,reason:'Confirmed procurement policy change'});
      return Response.json({error:{code:'VERSION_CONFLICT',message:'Read latest policy'}},{status:409});
    },
  }),error=>error instanceof APIError&&error.code==='VERSION_CONFLICT'&&error.status===409);
  assert.equal(calls,1);
});
test('scheduled policy time is preserved explicitly rather than changed to immediate',async()=>{
  const effective='2030-04-15T13:30:00.000Z';
  const result=await requestJSON('http://localhost','approver','/policy/versions',{method:'POST',
    body:{effective_at:effective},fetchImpl:async(_url,options)=>{
      assert.equal(JSON.parse(options.body).effective_at,effective);
      return Response.json({version:5,status:'scheduled',effective_at:effective});
    }});
  assert.equal(result.status,'scheduled');assert.equal(result.effective_at,effective);
});
