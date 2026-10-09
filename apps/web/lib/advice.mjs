/** Browser wait limits do not cancel a provider run or prove that it was not charged. */
export const ADVICE_READ_TIMEOUT_MS = 15_000;
export const ADVICE_WRITE_TIMEOUT_MS = 60_000;
const waitMessages = {
  ADVICE_WAIT_TIMEOUT: '等待建议服务超时',
  ADVICE_WAIT_CANCELLED: '已停止本页等待',
  ADVICE_CONTEXT_CHANGED: '来源或策略已变化，已停止本页等待',
};
export class AdviceWaitError extends Error {
  constructor(code) { super(`${code}：${waitMessages[code]}`); this.name = 'AdviceWaitError'; this.code = code; }
}

/** Fence late responses even if a fetch/proxy ignores abort; never retry a write. */
export async function waitForAdvice(operation, {signal, timeoutMs = ADVICE_READ_TIMEOUT_MS} = {}) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) throw new RangeError('Invalid advice wait limit');
  signal?.throwIfAborted();
  const controller = new AbortController();
  const forwardAbort = () => controller.abort(signal.reason);
  signal?.addEventListener('abort', forwardAbort, {once:true});
  let onAbort;
  const stopped = new Promise((_, reject) => {
    onAbort = () => reject(controller.signal.reason);
    controller.signal.addEventListener('abort', onAbort, {once:true});
  });
  const timer = setTimeout(() => controller.abort(new AdviceWaitError('ADVICE_WAIT_TIMEOUT')), timeoutMs);
  try {
    return await Promise.race([stopped, Promise.resolve().then(() => {
      controller.signal.throwIfAborted();
      return operation(controller.signal);
    })]);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
    controller.signal.removeEventListener('abort', onAbort);
  }
}

const failureMessages = {
  MODEL_TIMEOUT: '模型响应超时，提供方可能仍在处理，费用未知。',
  MODEL_CANCELLED: '服务端已停止等待；无法据此确认提供方已停止处理，费用未知。',
  MODEL_REFUSED: '模型拒绝提供本次建议，请人工核对请求与来源。',
  MODEL_EMPTY_OUTPUT: '模型未返回可用内容，未形成有效建议。',
  MODEL_OUTPUT_INCOMPLETE: '模型输出不完整或被过滤，未形成有效建议。',
  MODEL_PROTOCOL_INVALID: '模型响应不符合协议，未形成有效建议。',
  MODEL_SCHEMA_INVALID: '模型内容不符合建议格式，未形成有效建议。',
  MODEL_CALL_FAILED: '模型请求失败，调用结果与费用可能不确定。',
  BUDGET_EXCEEDED: '运行已达到调用次数、用量或时间限制，费用未知。',
};
export function adviceFailureExplanation(code) {
  return failureMessages[code] || '本次建议未成功完成，请核对已保存的运行状态。';
}

export function providerAttemptExplanation(output) {
  const attempts = output.provider_attempts;
  return Number.isSafeInteger(attempts) && attempts >= 0
    ? `提供方请求尝试 ${attempts} 次` : '提供方请求尝试次数未报告';
}
