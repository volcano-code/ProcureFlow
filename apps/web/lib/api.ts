export const API = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
export interface Identity {user_id:string; tenant_id:string; role:"buyer"|"approver"|"auditor"}
export interface Proposal {snapshot_hash:string; quote_id:string; total:string; quote_version:number; quote_values:QuoteValues}
export interface ProcurementRequest {id:string;title:string;sku:string;quantity:string;uom:string;budget:string;max_delivery_days:number;currency:string;version:number;status:string;proposal:Proposal|null}
export interface QuoteValues {supplier_id:string|null;sku:string|null;quantity:string|null;uom:string|null;unit_price:string|null;tax_mode:"included"|"excluded"|"unknown";tax_rate:string|null;shipping_cost:string|null;discount:string|null;delivery_days:number|null;currency:string|null}
export interface EvidenceRef {kind:string;fragment_id?:string;text?:string;reason?:string;page?:number;line?:number;row?:number;sheet?:string;cell_range?:string;document_sha256?:string}
export interface Quote {id:string;request_id:string;document_id:string;filename:string;version:number;version_id:string;values:QuoteValues;evidence:Record<string,EvidenceRef>;confirmed_by:string|null;calculation:{total:string|null;violations:string[];eligible:boolean}}
export interface AuditEvent {id:number;type:string;actor_id:string;created_at:string;payload:Record<string,unknown>}
export interface Operation {id:string;status:string;remote_id:string|null;error:string|null}
export interface Capabilities {mode:"demo"|"private";erp_mode:"mock"|"erpnext";demo_samples:boolean;erp_draft_writes_enabled:boolean;production_ready:boolean}
export {loadDemo, watchAudit} from "./transport.mjs";
import {requestJSON} from "./transport.mjs";
export function api<T>(path:string,token:string,method="GET",body?:unknown,signal?:AbortSignal):Promise<T> {
  return requestJSON<T>(API,token,path,{method,body,signal});
}
