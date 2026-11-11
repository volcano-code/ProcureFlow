/** AG-UI 1.0 read-only observation client. Never sends messages or invokes model tools.
 * This endpoint observes one already-persisted advice receipt; RUN_FINISHED
 * terminates the *observation* and does not indicate provider success.
 */
import {requestScope} from './session.mjs';

const accepted = new Set(['RUN_STARTED','STATE_SNAPSHOT','CUSTOM',
  'TEXT_MESSAGE_START','TEXT_MESSAGE_CONTENT','TEXT_MESSAGE_END','RUN_FINISHED']);
const MAX_FRAME = 65536;
export class AGUIError extends Error {
  constructor(code, status = 0) { super(code); this.code = code; this.status = status; this.name = 'AGUIError'; }
}
export class AGUIDecoder {
  constructor(expectedThread,expectedRun) {
    this.decoder = new TextDecoder(); this.buffer=''; this.thread=expectedThread; this.run=expectedRun;
    this.seenStart=false;this.seenSnapshot=false;this.finished=false;this.cursor=0;this.messageId=null;
  }
  feed(bytes) {
    if (this.finished) throw new AGUIError('AGUI_AFTER_TERMINAL');
    this.buffer += this.decoder.decode(bytes,{stream:true});
    if (new TextEncoder().encode(this.buffer).length>MAX_FRAME) throw new AGUIError('AGUI_FRAME_LIMIT');
    const events=[];
    for (;;) {
      const m=/\r?\n\r?\n/.exec(this.buffer);
      if (!m) break;
      const frame=this.buffer.slice(0,m.index);
      this.buffer=this.buffer.slice(m.index+m[0].length);
      const lines=frame.split(/\r?\n/);
      const data=lines.filter(x=>x.startsWith('data:')).map(x=>x.slice(5).trimStart()).join('\n');
      if (!data) continue;
      let event;
      try {event=JSON.parse(data);} catch {throw new AGUIError('AGUI_INVALID_JSON');}
      if (!event||typeof event!=='object'||!accepted.has(event.type)) throw new AGUIError('AGUI_UNSUPPORTED_EVENT');
      if (!this.seenStart) {
        if (event.type!=='RUN_STARTED'||event.threadId!==this.thread||event.runId!==this.run||event.protocolVersion!=='1.0')
          throw new AGUIError('AGUI_BAD_RUN');
        this.seenStart=true;
      } else if (!this.seenSnapshot) {
        if (event.type!=='STATE_SNAPSHOT') throw new AGUIError('AGUI_SNAPSHOT_REQUIRED');
        if (!event.snapshot||event.snapshot.requestId!==this.thread||event.snapshot.adviceRunId!==this.run||
            event.snapshot.sourceReadOnly!==true||event.snapshot.externalWritePermitted!==false||
            !Number.isSafeInteger(event.snapshot.auditCursor)||event.snapshot.auditCursor<0)
          throw new AGUIError('AGUI_INVALID_STATE');
        this.seenSnapshot=true;
        this.cursor=Math.max(this.cursor,event.snapshot.auditCursor);
      } else if (event.type==='RUN_FINISHED') {
        if (event.threadId!==this.thread||event.runId!==this.run||event.result?.mode!=='observation_only'||
            event.result?.providerSuccessNotImplied!==true||this.messageId)
          throw new AGUIError('AGUI_BAD_TERMINAL');
        this.finished=true;
      } else if (event.type==='CUSTOM') {
        if (event.name!=='procureflow.advice.progress'||!Number.isSafeInteger(event.value?.auditId)||
            event.value.auditId<1||typeof event.value.kind!=='string') throw new AGUIError('AGUI_BAD_PROGRESS');
      } else if (event.type==='TEXT_MESSAGE_START') {
        if (this.messageId||typeof event.messageId!=='string'||event.role!=='assistant') throw new AGUIError('AGUI_BAD_MESSAGE');
        this.messageId=event.messageId;
      } else if (event.type==='TEXT_MESSAGE_CONTENT') {
        if (!this.messageId||event.messageId!==this.messageId||typeof event.delta!=='string') throw new AGUIError('AGUI_BAD_MESSAGE');
      } else if (event.type==='TEXT_MESSAGE_END') {
        if (!this.messageId||event.messageId!==this.messageId) throw new AGUIError('AGUI_BAD_MESSAGE');
        this.messageId=null;
      } else throw new AGUIError('AGUI_INVALID_SEQUENCE');
      const id=lines.find(line=>line.startsWith('id:'));
      if (id) {
        const parsed=Number(id.slice(3).trim());
        if (!Number.isSafeInteger(parsed)||parsed<1||event.type!=='CUSTOM'||parsed!==event.value.auditId)
          throw new AGUIError('AGUI_BAD_CURSOR');
        this.cursor=Math.max(this.cursor,parsed);
      }
      events.push(event);
    }
    return events;
  }
  done() {
    if (!this.finished||!this.seenSnapshot||this.buffer.trim()) throw new AGUIError('AGUI_TRUNCATED');
  }
}
function relative(base, runId, after) {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(runId)||!Number.isSafeInteger(after)||after<0)
    throw new AGUIError('AGUI_BAD_INPUT');
  return base.replace(/\/$/,'') + '/api/v1/ag-ui/advice-runs/' + encodeURIComponent(runId) + '/observe?after=' + after;
}
export async function observeAdvice(base,credential,{runId,threadId,after=0,signal,fetchImpl=fetch,onEvent=()=>{}}) {
  if (typeof threadId!=='string'||!/^[a-zA-Z0-9_-]{1,128}$/.test(threadId)) throw new AGUIError('AGUI_BAD_INPUT');
  const scope=requestScope(credential,signal);
  const response=await fetchImpl(relative(base,runId,after),{
    method:'POST',headers:{Authorization:`Bearer ${scope.token}`,'Content-Type':'application/json',Accept:'text/event-stream'},
    body:JSON.stringify({threadId,runId,protocolVersion:'1.0',messages:[],tools:[],context:[],state:{}}),
    redirect:'error',credentials:'omit',cache:'no-store',signal:scope.signal,
  });
  scope.assertCurrent();
  if (!response.ok) {
    if (response.status===401) scope.invalidate();
    throw new AGUIError([401,403,404].includes(response.status)?'AGUI_DENIED':'AGUI_HTTP_ERROR',response.status);
  }
  if (!response.headers.get('content-type')?.startsWith('text/event-stream')||!response.body)
    throw new AGUIError('AGUI_NOT_SSE');
  const reader=response.body.getReader();const decoder=new AGUIDecoder(threadId,runId);
  let state=null;
  try {
    for (;;) {
      const {done,value}=await reader.read();
      scope.assertCurrent();
      if (done) break;
      for (const event of decoder.feed(value)) {
        scope.assertCurrent();
        if (event.type==='STATE_SNAPSHOT') state=event.snapshot;
        onEvent(event);
      }
    }
    decoder.done();
  } finally {try{await reader.cancel();}catch{} reader.releaseLock();}
  return {state,cursor:decoder.cursor};
}
