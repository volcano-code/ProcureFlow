import {requestJSON, APIError} from './transport.mjs';

/** Only explicit GET diagnostics. Independent lanes fence navigation and ignored aborts.
 * Identical in-flight reads share a task; completed reads are never cached or retried. */
export class RecoveryReader {
  #base; #credential; #fetchImpl; #tasks = new Map(); #closed = false;
  constructor(base, credential, {fetchImpl = fetch} = {}) {
    this.#base = base; this.#credential = credential; this.#fetchImpl = fetchImpl;
  }
  read(kind, id = null) {
    if (this.#closed) throw new DOMException('Recovery view closed', 'AbortError');
    if (!['list', 'detail', 'reconciliation'].includes(kind)) throw new Error('Unknown recovery read');
    if (kind !== 'list' && (typeof id !== 'string' || !id)) throw new Error('Operation ID required');
    const path = kind === 'list' ? `/recovery/operations?limit=50${id ? `&after=${encodeURIComponent(id)}` : ''}`
      : `/recovery/operations/${encodeURIComponent(id)}${kind === 'reconciliation' ? '/reconciliation' : ''}`;
    const previous = this.#tasks.get(kind);
    if (previous?.pending && previous.path === path) return previous;
    this.cancel(kind);
    const controller = new AbortController();
    const task = {path, controller, signal:controller.signal, pending:true, promise:null};
    task.promise = requestJSON(this.#base, this.#credential, path, {
      method:'GET', signal:controller.signal, fetchImpl:this.#fetchImpl,
    }).finally(() => {task.pending = false;});
    this.#tasks.set(kind, task);
    return task;
  }
  cancel(kind) {
    this.#tasks.get(kind)?.controller.abort(); this.#tasks.delete(kind);
  }
  close() {
    this.#closed = true;
    for (const kind of this.#tasks.keys()) this.cancel(kind);
  }
}

/** A readback cannot be presented against a different or no-longer-bound ledger. */
export function assertReconciliationBinding(result, detail) {
  if (!result || result.operation_id !== detail.id || !detail.ledger_sha256 || result.ledger_sha256 !== detail.ledger_sha256)
    throw new APIError('RECOVERY_BINDING_CHANGED', '核对结果与当前操作账本不一致，请重新读取详情。');
  if (result.replay_permitted !== false || result.external_write_attempted !== false ||
      !['verified', 'missing', 'mismatch', 'blocked', 'unavailable'].includes(result.status) ||
      typeof result.simulated !== 'boolean' || typeof result.network_attempted !== 'boolean' ||
      typeof result.matches_snapshot !== 'boolean' || typeof result.draft_verified !== 'boolean' ||
      !result.expected || typeof result.expected !== 'object' || Array.isArray(result.expected) ||
      (result.observed !== null && (!result.observed || typeof result.observed !== 'object' || Array.isArray(result.observed))) ||
      !Array.isArray(result.differences) ||
      (result.status === 'verified' && (result.matches_snapshot !== true || result.draft_verified !== true ||
        result.observed === null || result.differences.length !== 0)))
    throw new APIError('INVALID_RECOVERY_RESPONSE', '服务端未返回一致的只读诊断结果，请联系操作员核验。');
  return result;
}

export function reconciliationMessage(status) {
  return {
    verified:'快照与远端草稿一致；此结果仅供诊断，不授予重放权限。',
    missing:'未找到远端记录。查无记录不能证明未提交，仍禁止重放。',
    mismatch:'远端记录与原快照不一致，请人工核查差异，禁止重放。',
    blocked:'本次核对被阻止；不能据此判断远端提交结果，禁止重放。',
    unavailable:'本次无法完成核对；远端结果仍不确定，禁止重放。',
  }[status] || '诊断状态未知；远端结果仍不确定，禁止重放。';
}
