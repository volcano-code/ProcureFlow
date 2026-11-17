import type {Credential} from './session.mjs';
import type {RecoveryDetail,RecoveryPage,RecoveryReconciliation} from './api';
export interface RecoveryReadTask<T> {readonly signal:AbortSignal;readonly pending:boolean;readonly promise:Promise<T>}
export class RecoveryReader {
  constructor(base:string,credential:Credential,options?:{fetchImpl?:typeof fetch});
  read(kind:'list',after?:string|null):RecoveryReadTask<RecoveryPage>;
  read(kind:'detail',id:string):RecoveryReadTask<RecoveryDetail>;
  read(kind:'reconciliation',id:string):RecoveryReadTask<RecoveryReconciliation>;
  cancel(kind:'list'|'detail'|'reconciliation'):void;
  close():void;
}
export function assertReconciliationBinding(result:RecoveryReconciliation,detail:RecoveryDetail):RecoveryReconciliation;
export function reconciliationMessage(status:string):string;
