import type {Credential} from './session.mjs';
export class AGUIError extends Error {code:string;status:number;constructor(code:string,status?:number)}
export class AGUIDecoder {
  constructor(expectedThread:string,expectedRun:string);
  cursor:number; finished:boolean;
  feed(bytes:Uint8Array):Array<{type:string;[key:string]:unknown}>;
  done():void;
}
export function observeAdvice(base:string,credential:Credential,opts:{
  runId:string;threadId:string;after?:number;signal?:AbortSignal;fetchImpl?:typeof fetch;
  onEvent?:(event:{type:string;[key:string]:any})=>void;
}):Promise<{state:{agentStatus:string;current:boolean;humanAction:string;auditCursor:number}|null;cursor:number}>;
