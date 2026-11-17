export const ADVICE_READ_TIMEOUT_MS:number;
export const ADVICE_WRITE_TIMEOUT_MS:number;
export class AdviceWaitError extends Error {
  readonly code:'ADVICE_WAIT_TIMEOUT'|'ADVICE_WAIT_CANCELLED'|'ADVICE_CONTEXT_CHANGED';
  constructor(code:'ADVICE_WAIT_TIMEOUT'|'ADVICE_WAIT_CANCELLED'|'ADVICE_CONTEXT_CHANGED');
}
export function waitForAdvice<T>(operation:(signal:AbortSignal)=>Promise<T>,options?:{signal?:AbortSignal;timeoutMs?:number}):Promise<T>;
export function adviceFailureExplanation(code:string):string;
export function providerAttemptExplanation(output:{provider_attempts?:number}):string;
