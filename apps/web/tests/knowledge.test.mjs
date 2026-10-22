import test from 'node:test';
import assert from 'node:assert/strict';
import {knowledgeBinding,citationPath,latestDocumentVersion} from '../lib/knowledge.mjs';
const hash='a'.repeat(64);
const snapshot={knowledge:{corpus_hash:hash,scope:'procurement',documents:[{id:'kv_1',content_hash:hash,chunks:[{id:'kv_1:0',text:'not HTML <script>'}]}]}};
test('binding requires an exact captured source',()=>{
  assert.equal(knowledgeBinding(snapshot,'kv_1:0').text,'not HTML <script>');
  assert.equal(knowledgeBinding(snapshot,'kv_2:0'),null);
  assert.equal(knowledgeBinding({},'kv_1:0'),null);
});
test('malformed snapshots do not create a citation',()=>{
  for(const value of [null,{},[],{knowledge:{}},{knowledge:{corpus_hash:'bad',documents:[]}}])assert.equal(knowledgeBinding(value,'id'),null);
});
test('citation path encodes all authority-adjacent input',()=>{
  assert.match(citationPath('x/../execute',hash),/x%2F\.\.%2Fexecute/);
  assert.match(citationPath('kv_1:0',hash,'a&tenant=other'),/scope=a%26tenant%3Dother/);
});
test('CAS includes latest draft, not just currently active source',()=>{
  const rows=[{document_key:'a',latest_version:3},{document_key:'b',latest_version:8}];
  assert.equal(latestDocumentVersion(rows,'a'),3);assert.equal(latestDocumentVersion(rows,'c'),0);
});
