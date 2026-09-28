/** Shared browser/Node transport. No automatic retry for business mutations. */
export class APIError extends Error {
  constructor(code, message, status = 0) {
    super(`${code}: ${message}`);
    this.name = 'APIError'; this.code = code; this.status = status;
  }
}
function endpoint(base, path) {
  if (!path.startsWith('/') || path.startsWith('//')) throw new APIError('INVALID_PATH', 'Relative API path required');
  return base.replace(/\/$/, '') + '/api/v1' + path;
}
export async function requestJSON(base, token, path, {method = 'GET', body, signal, fetchImpl = fetch} = {}) {
  const form = body instanceof FormData;
  const response = await fetchImpl(endpoint(base, path), {
    method, signal, cache: 'no-store', redirect: 'error',
    headers: {Authorization: `Bearer ${token}`, ...(body !== undefined && !form ? {'Content-Type': 'application/json'} : {})},
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  });
  let data;
  try { data = await response.json(); }
  catch { throw new APIError('INVALID_RESPONSE', 'API response is not JSON', response.status); }
  if (!response.ok) {
    const error = data && typeof data === 'object' ? data.error : null;
    throw new APIError(error?.code || 'HTTP_ERROR', error?.message || `HTTP ${response.status}; check the submitted fields`, response.status);
  }
  return data;
}
/** Creates one new request. Individual imports are idempotent by document hash.
 * Partial failure retains its request ID; it never silently creates another request.
 */
export async function loadDemo(base, token, {signal, fetchImpl = fetch, onCreated = () => {}} = {}) {
  const opts = {signal, fetchImpl};
  const sample = await requestJSON(base, token, '/demo/samples', opts);
  if (sample.synthetic !== true || !Array.isArray(sample.files) || sample.files.length !== 3) {
    throw new APIError('INVALID_DEMO', 'Expected three explicitly synthetic samples');
  }
  const request = await requestJSON(base, token, '/requests', {...opts, method: 'POST', body: sample.request});
  onCreated(request.id);
  for (const filename of sample.files) {
    try {
      const response = await fetchImpl(endpoint(base, `/demo/samples/${encodeURIComponent(filename)}`), {
        headers: {Authorization: `Bearer ${token}`}, signal, redirect: 'error', cache: 'no-store',
      });
      if (!response.ok) throw new APIError('SAMPLE_FETCH_FAILED', `HTTP ${response.status}`, response.status);
      const form = new FormData(); form.append('file', await response.blob(), filename);
      await requestJSON(base, token, `/requests/${request.id}/documents`, {...opts, method: 'POST', body: form});
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new APIError('DEMO_PARTIAL', `Request ${request.id} retained; upload ${filename} manually. ${error instanceof Error ? error.message : 'Import failed'}`);
    }
  }
  return request.id;
}
/** Incremental UTF-8 SSE decoding. EOF never commits an unfinished event. */
export class AuditDecoder {
  constructor(maxBufferedBytes = 131072) {
    this.decoder = new TextDecoder(); this.buffer = ''; this.max = maxBufferedBytes;
  }
  feed(bytes) {
    this.buffer += this.decoder.decode(bytes, {stream: true});
    if (new TextEncoder().encode(this.buffer).length > this.max) throw new APIError('STREAM_LIMIT', 'Audit event buffer exceeded limit');
    const events = []; let match;
    while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
      const frame = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (!data) continue;
      let event;
      try { event = JSON.parse(data); } catch { throw new APIError('STREAM_INVALID', 'Audit event is not valid JSON'); }
      if (!event || !Number.isSafeInteger(event.id) || event.id <= 0 || typeof event.type !== 'string') {
        throw new APIError('STREAM_INVALID', 'Audit event identity is malformed');
      }
      events.push(event);
    }
    return events;
  }
}
function sleep(ms, signal) {
  return new Promise(resolve => {
    if (signal.aborted) { resolve(); return; }
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms); signal.addEventListener('abort', done, {once: true});
  });
}
export async function watchAudit(base, token, requestId, {signal, after = 0, onEvent, onStatus = () => {}, fetchImpl = fetch, retryMs = 1500}) {
  let cursor = after;
  while (!signal.aborted) {
    let reader;
    try {
      onStatus('connecting');
      const response = await fetchImpl(endpoint(base, `/requests/${encodeURIComponent(requestId)}/events/stream?after=${cursor}`), {
        headers: {Authorization: `Bearer ${token}`}, signal, cache: 'no-store', redirect: 'error',
      });
      if (!response.ok) {
        if ([401, 403, 404].includes(response.status)) { onStatus('denied'); return; }
        throw new APIError('STREAM_HTTP', `HTTP ${response.status}`, response.status);
      }
      if (!response.body) throw new APIError('STREAM_EMPTY', 'Response stream missing');
      reader = response.body.getReader(); const decoder = new AuditDecoder(); onStatus('live');
      for (;;) {
        const {done, value} = await reader.read();
        if (done || signal.aborted) break;
        for (const event of decoder.feed(value)) {
          if (signal.aborted) break;
          if (event.id > cursor) { cursor = event.id; onEvent(event); }
        }
      }
    } catch { if (signal.aborted) return; }
    finally { if (reader) { try { await reader.cancel(); } catch {} reader.releaseLock(); } }
    if (!signal.aborted) { onStatus('retrying'); await sleep(retryMs, signal); }
  }
}
