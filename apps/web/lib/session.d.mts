export type SessionReason = 'expired'|'invalid'|'logout'|'navigation'|'replaced'|'cancelled';
export class SessionScope {
  readonly id:number; readonly expiresAt:string|null; readonly signal:AbortSignal; readonly active:boolean;
  constructor(token:string,options?:{expiresAt?:string|null;onInvalidated?:(reason:SessionReason)=>void});
  assertCurrent():void; authorization():string; invalidate(reason?:SessionReason):void; toString():string;
}
export type Credential = string|SessionScope;
export function requestScope(credential:Credential,signal?:AbortSignal):{token:string;signal:AbortSignal|undefined;assertCurrent():void;invalidate():void};
