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
export interface RequestLine {sku:string;quantity:string;uom:string}
export interface ProcurementRequest {id:string;title:string;sku:string|null;quantity:string|null;uom:string|null;lines?:RequestLine[]|null;budget:string;max_delivery_days:number;currency:string;version:number;status:string;proposal:Proposal|null;proposal_current:boolean;proposal_stale_reason:string|null}
export interface QuoteLineValues {sku:string|null;quantity:string|null;uom:string|null;unit_price:string|null;tax_mode:"included"|"excluded"|"unknown";tax_rate:string|null;discount:string|null;delivery_days:number|null}
export type QuoteScalarField = Exclude<keyof QuoteValues,"lines">;
export interface QuoteValues {lines?:QuoteLineValues[]|null;supplier_id:string|null;sku:string|null;quantity:string|null;uom:string|null;unit_price:string|null;tax_mode:"included"|"excluded"|"unknown";tax_rate:string|null;shipping_cost:string|null;discount:string|null;delivery_days:number|null;currency:string|null}
export interface EvidenceRef {sources?:EvidenceRef[];kind:string;fragment_id?:string;text?:string;reason?:string;page?:number;line?:number;row?:number;sheet?:string;cell_range?:string;document_sha256?:string}
export interface DocumentEvidence {id:string;filename:string;sha256:string;trust:string;fragments:{id:string;text:string;locator:EvidenceRef}[]}
export interface LineCalculation {sku:string|null;total:string|null;goods?:string|null;discount?:string|null;added_tax?:string|null;violations?:string[];missing?:string[];errors?:string[]}
export interface QuoteCalculation {total:string|null;violations:string[];eligible:boolean;goods?:string|null;discount?:string|null;added_tax?:string|null;shipping?:string|null;lines?:LineCalculation[];coverage?:{requested_skus:string[];quoted_skus:string[];missing_skus:string[];unexpected_skus:string[];complete:boolean};effective_limits?:{budget:string;max_delivery_days:number}}
export interface Quote {id:string;request_id:string;document_id:string;filename:string;version:number;version_id:string;values:QuoteValues;evidence:Record<string,EvidenceRef>;confirmed_by:string|null;calculation:QuoteCalculation}
export interface AuditEvent {id:number;type:string;actor_id:string;created_at:string;payload:Record<string,unknown>}
export interface Operation {id:string;status:string;remote_id:string|null;error:string|null}
export interface RecoveryOperation extends Operation {request_id:string;request_title:string;attempts:number;snapshot_hash:string;
  created_at:string;outbox_status:string|null;hold:{restore_id:string;original_status:string;created_at:string};replay_permitted:false}
export interface RecoveryPage {state:{state:string;generation:number;required_auth_mode:string|null;restore_id:string|null};
  items:RecoveryOperation[];next_after:string|null;total:number}
export interface RecoveryDetail extends RecoveryOperation {payload_sha256:string;ledger_sha256:string;
  expected:Record<string,unknown>;approval:{id:string;status:string;expires_at:string;authority_current:boolean;snapshot_matches:boolean;unexpired:boolean;authority_evaluated:boolean}|null;
  sources:{id:string|null;filename:string;sha256:string|null;integrity:"verified"|"missing"|"mismatch"|"unavailable";quote_id:string|null;quote_version:number|null}[];diagnostics:string[]}
export interface RecoveryReconciliation {operation_id:string;ledger_sha256:string;verified_at:string;
  status:"verified"|"missing"|"mismatch"|"blocked"|"unavailable";reason?:string;simulated:boolean;network_attempted:boolean;
  external_write_attempted:false;replay_permitted:false;remote_id:string|null;expected:Record<string,unknown>;observed:Record<string,unknown>|null;
  differences:{field:string;expected:unknown;observed:unknown;reason:string}[];matches_snapshot:boolean;draft_verified:boolean}
export interface Capabilities {mode:"demo"|"private"|"pilot";erp_mode:"mock"|"erpnext";demo_samples:boolean;erp_draft_writes_enabled:boolean;production_ready:boolean;advice_configured:boolean;advice_runtime:"langgraph-read-only-v1";advice_runtime_version:string}
export interface AdviceOutput {
  summary:string;evidence_ids:string[];runtime:string;llm_used:boolean;advisory_only:true;
  semantic_factuality_verified:false;evidence_read_verified?:boolean;model_calls:number;provider_attempts?:number;tool_calls:number;
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
export interface TableImportSheet {name:string;rows:{row:number;cells:TableImportCell[]}[];suggested_header_row:number|null;suggested_mapping:Partial<Record<QuoteScalarField,string>>}
export interface TableImportSelection {sheet:string;header_row:number;row?:number;rows?:number[];mapping:Partial<Record<QuoteScalarField,string>>}
export interface TableImportPreview {id:string;request_id:string;revision:number;status:"OPEN"|"IMPORTED"|"ARCHIVED";expires_at:string;
  filename:string;document_sha256:string;sheets:TableImportSheet[];selection:TableImportSelection|null;
  suggested_mapping:Partial<Record<QuoteScalarField,string>>;values:QuoteValues|null;evidence:Record<string,EvidenceRef>|null;
  issues:string[];can_confirm:boolean;quote_id:string|null}
