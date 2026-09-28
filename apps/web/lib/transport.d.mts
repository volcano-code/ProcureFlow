export class APIError extends Error {code: string; status: number; constructor(code:string, message:string, status?:number)}
export interface RequestOptions {method?:string; body?:unknown; signal?:AbortSignal; fetchImpl?:typeof fetch}
export function requestJSON<T=unknown>(base:string,token:string,path:string,options?:RequestOptions):Promise<T>;
export function loadDemo(base:string,token:string,options?:{signal?:AbortSignal;fetchImpl?:typeof fetch;onCreated?:(id:string)=>void}):Promise<string>;
export class AuditDecoder {constructor(maxBufferedBytes?:number); feed(bytes:Uint8Array):import('./api').AuditEvent[]}
export function watchAudit(base:string,token:string,requestId:string,options:{signal:AbortSignal;after?:number;onEvent:(event:import('./api').AuditEvent)=>void;onStatus?:(status:string)=>void;fetchImpl?:typeof fetch;retryMs?:number}):Promise<void>;
