import test from 'node:test';
import assert from 'node:assert/strict';
import {requestJSON, APIError, AuditDecoder, watchAudit, loadDemo} from '../lib/transport.mjs';
const encode = text=>new TextEncoder().encode(text);

test('preserves decimal strings and authenticates request without mutation retries', async()=>{
  let calls=0;
  const fetchImpl=async(url,options)=>{
    calls++; assert.equal(url,'http://localhost:8000/api/v1/requests');
    assert.equal(options.headers.Authorization,'Bearer test');
    assert.equal(JSON.parse(options.body).quantity,'20.125');
    return Response.json({error:{code:'VERSION_CONFLICT',message:'Refresh first'}},{status:409});
  };
  await assert.rejects(()=>requestJSON('http://localhost:8000','test','/requests',{method:'PUT',body:{quantity:'20.125'},fetchImpl}),
    error=>error instanceof APIError&&error.status===409&&error.code==='VERSION_CONFLICT');
  assert.equal(calls,1);
});
test('HTML error never masquerades as successful JSON',async()=>{
  await assert.rejects(()=>requestJSON('http://localhost','t','/me',{fetchImpl:async()=>new Response('<h1>error</h1>',{status:502})}),
    error=>error.code==='INVALID_RESPONSE');
});
test('passes FormData without overwriting multipart boundary',async()=>{
  const form=new FormData();form.append('file',new Blob(['a']),'a.txt');
  await requestJSON('http://localhost','t','/documents',{method:'POST',body:form,fetchImpl:async(_,options)=>{
    assert.equal(options.headers['Content-Type'],undefined);assert.equal(options.body,form);return Response.json({ok:true});
  }});
});
test('passes abort signals to fetch',async()=>{
  const controller=new AbortController();controller.abort();
  await assert.rejects(()=>requestJSON('http://localhost','t','/me',{signal:controller.signal,fetchImpl:async(_,options)=>{
    assert.equal(options.signal,controller.signal);options.signal.throwIfAborted();
  }}),{name:'AbortError'});
});
test('refuses cross-origin API path injection',async()=>{
  await assert.rejects(()=>requestJSON('http://localhost','t','//evil.test'),error=>error.code==='INVALID_PATH');
});
test('UTF-8 and CRLF frames may be split at every byte',()=>{
  const bytes=encode(': heartbeat\r\n\r\nid: 7\r\ndata: {"id":7,"type":"报价已确认"}\r\n\r\n');
  const decoder=new AuditDecoder();const events=[];
  for(const byte of bytes) events.push(...decoder.feed(Uint8Array.of(byte)));
  assert.deepEqual(events,[{id:7,type:'报价已确认'}]);
});
test('multiline JSON event and several frames per packet',()=>{
  const d=new AuditDecoder();
  assert.deepEqual(d.feed(encode('data: {"id":1,\ndata: "type":"A"}\n\ndata: {"id":2,"type":"B"}\n\n')),
    [{id:1,type:'A'},{id:2,type:'B'}]);
});
test('unfinished final event is not committed',()=>{
  assert.deepEqual(new AuditDecoder().feed(encode('data: {"id":1,"type":"A"}')),[]);
});
test('malformed audit event is rejected',()=>{
  assert.throws(()=>new AuditDecoder().feed(encode('data: {"id":"fake","type":"A"}\n\n')),e=>e.code==='STREAM_INVALID');
});
test('bounded SSE buffering',()=>{
  assert.throws(()=>new AuditDecoder(8).feed(encode('abcdefghijkl')),e=>e.code==='STREAM_LIMIT');
});
test('SSE reconnect uses cursor and de-duplicates replays',async()=>{
  const abort=new AbortController(),events=[],urls=[];
  await watchAudit('http://localhost','t','req-1',{signal:abort.signal,retryMs:1,
    fetchImpl:async(url)=>{urls.push(url);return new Response(encode(urls.length===1?
      'data: {"id":1,"type":"A"}\n\n':'data: {"id":1,"type":"A"}\n\ndata: {"id":2,"type":"B"}\n\n'));},
    onEvent:e=>{events.push(e);if(e.id===2)abort.abort();}});
  assert.deepEqual(events.map(e=>e.id),[1,2]);assert.ok(urls[1].endsWith('after=1'));
});
test('SSE denial terminates rather than retrying forever',async()=>{
  const statuses=[];let count=0;
  await watchAudit('http://localhost','t','req',{signal:new AbortController().signal,
    fetchImpl:async()=>{count++;return new Response('',{status:403});},onEvent:()=>assert.fail(),onStatus:s=>statuses.push(s)});
  assert.equal(count,1);assert.equal(statuses.at(-1),'denied');
});
test('partial demo failure reports retained request instead of silently recreating it',async()=>{
  const created=[];let creates=0;
  await assert.rejects(()=>loadDemo('http://localhost','t',{onCreated:id=>created.push(id),fetchImpl:async(url)=>{
    if(url.endsWith('/demo/samples'))return Response.json({synthetic:true,request:{title:'test'},files:['a','b','c']});
    if(url.endsWith('/requests')){creates++;return Response.json({id:'retained-123'});}
    return new Response('',{status:503});
  }}),e=>e.code==='DEMO_PARTIAL'&&e.message.includes('retained-123'));
  assert.deepEqual(created,['retained-123']);assert.equal(creates,1);
});

test('cancellation stops remaining events in the same SSE chunk', async () => {
  const controller = new AbortController(); const seen = [];
  await watchAudit('http://local.test', 'test-token', 'r1', {
    signal:controller.signal,
    fetchImpl:async()=>new Response(new ReadableStream({start(stream) {
      stream.enqueue(new TextEncoder().encode('data: {"id":1,"type":"A"}\n\ndata: {"id":2,"type":"B"}\n\n'));
      stream.close();
    }})),
    onEvent:event=>{seen.push(event.id);controller.abort();},
  });
  assert.deepEqual(seen, [1]);
});
