import type {Credential} from "./session.mjs";
export {SessionScope} from "./session.mjs";
export type {Credential} from "./session.mjs";
export const API = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
export interface Identity {user_id:string; tenant_id:string; role:"buyer"|"approver"|"auditor";expires_at?:string}
export interface PolicyVersion {id:string;tenant_id:string;version:number;budget_cap:string|null;max_delivery_days:number|null;
  minimum_valid_quotes:number;effective_at:string;created_at:string;created_by:string;reason:string;
  clauses:{id:string;text:string}[];policy_hash:string;latest_version:number;status:"effective"|"scheduled"|"superseded"}
export interface Proposal {snapshot_hash:string; quote_id:string; total:string; quote_version:number; quote_values:QuoteValues;
  policy_version:number;policy_hash:string;policy:Omit<PolicyVersion,"latest_version"|"status">;quote_collection:Quote[];quote_collection_hash:string}
export interface Evaluation {id:string;request_id:string;request_version:number;created_at:string;created_by:string;
  policy_version:number;policy_hash:string;quote_collection_hash:string;input_hash:string;input_snapshot:Record<string,unknown>;
  result:{quotes:Quote[];proposal:Proposal|null;violations:string[];valid_quote_count:number;minimum_valid_quotes:number};
  current:boolean;stale_reason:string|null;[key:string]:unknown}
export interface ApprovalReceipt {id:string;request_id:string;approver_id:string;snapshot_hash:string;snapshot:Proposal|null;
  status:string;stored_status:string;note:string;created_at:string;expires_at:string;current:boolean;stale_reason:string|null}
export interface ProcurementRequest {id:string;title:string;sku:string;quantity:string;uom:string;budget:string;max_delivery_days:number;currency:string;version:number;status:string;proposal:Proposal|null;proposal_current:boolean;proposal_stale_reason:string|null}
export interface QuoteValues {supplier_id:string|null;sku:string|null;quantity:string|null;uom:string|null;unit_price:string|null;tax_mode:"included"|"excluded"|"unknown";tax_rate:string|null;shipping_cost:string|null;discount:string|null;delivery_days:number|null;currency:string|null}
export interface EvidenceRef {kind:string;fragment_id?:string;text?:string;reason?:string;page?:number;line?:number;row?:number;sheet?:string;cell_range?:string;document_sha256?:string}
export interface DocumentEvidence {id:string;filename:string;sha256:string;trust:string;fragments:{id:string;text:string;locator:EvidenceRef}[]}
export interface Quote {id:string;request_id:string;document_id:string;filename:string;version:number;version_id:string;values:QuoteValues;evidence:Record<string,EvidenceRef>;confirmed_by:string|null;calculation:{total:string|null;violations:string[];eligible:boolean;effective_limits?:{budget:string;max_delivery_days:number}}}
export interface AuditEvent {id:number;type:string;actor_id:string;created_at:string;payload:Record<string,unknown>}
export interface Operation {id:string;status:string;remote_id:string|null;error:string|null}
export interface Capabilities {mode:"demo"|"private"|"pilot";erp_mode:"mock"|"erpnext";demo_samples:boolean;erp_draft_writes_enabled:boolean;production_ready:boolean;advice_configured:boolean;advice_runtime:"langgraph-read-only-v1";advice_runtime_version:string}
export interface AdviceOutput {
  summary:string;evidence_ids:string[];runtime:string;llm_used:boolean;advisory_only:true;
  semantic_factuality_verified:false;evidence_read_verified?:boolean;model_calls:number;tool_calls:number;
  trace:Record<string,unknown>[];usage:{prompt_tokens:number;completion_tokens:number;total_tokens:number}|null;
  usage_complete:boolean;cost:null;
}
export interface AdviceRun {
  id:string;request_id:string;request_version:number;status:"PENDING"|"RUNNING"|"COMPLETED"|"FAILED"|"INTERRUPTED"|"STALE";
  input_hash:string;policy_version?:number;policy_hash?:string;quote_collection_hash?:string;input_snapshot?:Record<string,unknown>;created_at:string;started_at:string|null;completed_at:string|null;
  error_code:string|null;output:AdviceOutput|null;current:boolean;stale_reason:string|null;
}
export {loadDemo, watchAudit} from "./transport.mjs";
import {requestJSON} from "./transport.mjs";
export function api<T>(path:string,token:Credential,method="GET",body?:unknown,signal?:AbortSignal):Promise<T> {
  return requestJSON<T>(API,token,path,{method,body,signal});
}

export interface TableImportCell {column:string;cell:string;value:string|null;formula?:boolean}
export interface TableImportSheet {name:string;rows:{row:number;cells:TableImportCell[]}[];suggested_header_row:number|null;suggested_mapping:Partial<Record<keyof QuoteValues,string>>}
export interface TableImportSelection {sheet:string;header_row:number;row:number;mapping:Partial<Record<keyof QuoteValues,string>>}
export interface TableImportPreview {id:string;request_id:string;revision:number;status:"OPEN"|"IMPORTED";expires_at:string;
  filename:string;document_sha256:string;sheets:TableImportSheet[];selection:TableImportSelection|null;
  suggested_mapping:Partial<Record<keyof QuoteValues,string>>;values:QuoteValues|null;evidence:Record<string,EvidenceRef>|null;
  issues:string[];can_confirm:boolean;quote_id:string|null}
