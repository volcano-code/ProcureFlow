"use client";
import {useCallback,useEffect,useRef,useState} from "react";
import {api,type Capabilities,type ProcurementRequest,type QuoteValues,type SessionScope} from "@/lib/api";

type SourceRef={fragment_id:string;text:string;document_sha256:string;page?:number;line?:number;origin:string};
type CandidateRun={id:string;revision:number;status:string;filename:string;can_import:boolean;current:boolean;
  expired:boolean;error_code:string|null;stale_reason:string|null;quote_id:string|null;created_at:string;
  output:{summary:string;values:QuoteValues;evidence:Record<string,SourceRef[]>;
    clarifications:{field:string;question:string;reason:string}[];
    calculation:{total:string|null;missing:string[];violations:string[]};model_calls:number;tool_calls:number;
    usage_complete:boolean;semantic_factuality_verified:false;output_hash:string}|null};
const status:Record<string,string>={PENDING:"已预留，未调用模型",RUNNING:"提取中",NEEDS_REVIEW:"等待人工核对",
  NEEDS_CLARIFICATION:"有缺失或冲突，待补充",IMPORTED:"已导入未确认报价",CANCELLED:"已取消",FAILED:"提取失败",
  STALE:"输入版本已失效",INTERRUPTED:"执行中断，禁止自动重放"};
const labels:Record<string,string>={supplier_id:"供应商",sku:"物料标识",quantity:"数量",uom:"单位",unit_price:"单价",
  tax_mode:"税费模式",tax_rate:"税率",shipping_cost:"运费",discount:"折扣",delivery_days:"交期（天）",currency:"币种"};

/** Keyed by identity, request ID and version. Unmount aborts browser waits only, never asserts server cancellation. */
export default function CandidatePanel({request,token,cap,buyer,disabled,onImported}:{request:ProcurementRequest;
  token:SessionScope;cap:Capabilities;buyer:boolean;disabled:boolean;onImported:()=>void}) {
  const [runs,setRuns]=useState<CandidateRun[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const [allowModel,setAllowModel]=useState(false),[ack,setAck]=useState(false),[reason,setReason]=useState("");
  const input=useRef<HTMLInputElement>(null),active=useRef(true),lock=useRef(false),epoch=useRef(0);
  const controllers=useRef(new Set<AbortController>());
  const call=useCallback(async<T,>(path:string,method="GET",body?:unknown):Promise<T>=>{
    const controller=new AbortController();controllers.current.add(controller);
    try {return await api<T>(path,token,method,body,controller.signal);}
    finally {controllers.current.delete(controller);}
  },[token]);
  const refresh=useCallback(async()=>{
    const n=++epoch.current;const data=await call<CandidateRun[]>(`/requests/${request.id}/candidate-runs`);
    if(active.current&&token.active&&n===epoch.current)setRuns(data);
  },[call,request.id,token]);
  useEffect(()=>{active.current=true;void refresh().catch(e=>{if(active.current&&token.active)setError(String(e));});
    return()=>{active.current=false;epoch.current++;controllers.current.forEach(c=>c.abort());};
  },[refresh,token]);
  async function act(work:()=>Promise<void>) {
    if(lock.current||!token.active)return;lock.current=true;setBusy(true);setError("");
    try {await work();}
    catch(e){if(active.current&&token.active)setError(e instanceof Error?e.message:String(e));}
    finally {lock.current=false;if(active.current&&token.active)setBusy(false);}
  }
  const multi=!!request.lines?.length;
  return <section className="panel candidate-panel" data-testid="candidate-panel" aria-label="非标准报价候选核对">
    <h2>非标准报价 · 候选核对</h2>
    <p>上传 TXT 或文本 PDF，提取结果只供人工核对。引用位置有效不等于字段语义正确，采纳后仍须确认报价并独立审批。</p>
    {multi?<p role="note">当前候选入口仅支持单物料；多物料请使用表格映射。</p>:null}
    {!cap.candidate_model_configured?<p role="note">服务端未启用候选模型。可保存原文和预留任务，但不会自动调用模型或产生费用。</p>:null}
    <input ref={input} type="file" hidden accept=".txt,.pdf" data-testid="candidate-file" onChange={e=>{
      const file=e.target.files?.[0];e.target.value="";if(!file)return;
      void act(async()=>{
        const data=new FormData();data.append("file",file);
        const doc=await call<{id:string}>(`/requests/${request.id}/candidate-sources`,"POST",data);
        if(!active.current||!token.active)return;
        await call(`/requests/${request.id}/candidate-runs`,"POST",{document_id:doc.id,
          expected_version:request.version,idempotency_key:crypto.randomUUID()});await refresh();
      });
    }}/>
    <button type="button" disabled={!buyer||disabled||multi||busy} onClick={()=>input.current?.click()}>上传候选原文</button>
    <button type="button" disabled={busy} onClick={()=>void act(refresh)}>刷新候选任务</button>
    <label><input type="checkbox" checked={allowModel} onChange={e=>setAllowModel(e.target.checked)}/>
      我允许将本任务原文发送给服务端配置的模型；可能产生费用</label>
    {error?<p role="alert">{error}</p>:null}
    {runs.map(run=><article key={run.id} className="candidate-run" data-testid="candidate-run">
      <h3>{run.filename} · {status[run.status]||run.status}</h3><small>{run.id} · 版本 {run.revision}</small>
      {!run.current||run.expired?<p role="alert">输入已变化或任务已过期，不能采纳。请基于最新需求重新预留。</p>:null}
      {run.error_code?<p role="alert">{run.error_code}。失败任务不会自动重放；费用未知时不按零统计。</p>:null}
      {run.status==="PENDING"?<button type="button" disabled={!buyer||disabled||busy||!allowModel||!cap.candidate_model_configured||!run.current||run.expired}
        onClick={()=>void act(async()=>{await call(`/candidate-runs/${run.id}/process`,"POST",{
          expected_revision:run.revision,acknowledge_external_model:true});await refresh();})}>开始候选提取</button>:null}
      {["PENDING","RUNNING"].includes(run.status)?<button type="button" disabled={!buyer||disabled} onClick={()=>{
        // Cancellation remains available while the synchronous model request is awaiting.
        void call<CandidateRun>(`/candidate-runs/${run.id}`).then(current=>{
          if(!active.current||!token.active)return;
          return call(`/candidate-runs/${run.id}/cancel`,"POST",{expected_revision:current.revision});
        }).then(()=>{if(active.current&&token.active)return refresh();})
          .catch(e=>{if(active.current&&token.active)setError(String(e));});
      }}>取消候选任务</button>:null}
      {run.output?<>
        <p>{run.output.summary}</p>
        <p>候选计算总额：{run.output.calculation.total??"未知"} · 模型调用 {run.output.model_calls} 次 · 工具 {run.output.tool_calls} 次 · 费用未验证</p>
        <div className="table-scroll"><table><thead><tr><th>字段</th><th>候选值（未确认）</th><th>原文依据</th></tr></thead><tbody>
          {Object.entries(labels).map(([field,label])=><tr key={field}><td>{label}</td>
            <td>{String(run.output!.values[field as keyof QuoteValues]??"未知")}</td><td>
              {(run.output!.evidence[field]||[]).map(ref=><details key={ref.fragment_id}><summary>{ref.fragment_id}</summary>
                <p>{ref.text}</p><small>页 {ref.page??"—"} / 行 {ref.line??"—"} · SHA-256 {ref.document_sha256}</small></details>)}
            </td></tr>)}
        </tbody></table></div>
        {run.output.clarifications.length?<div role="note"><h4>需要补充</h4>{run.output.clarifications.map(q=><p key={q.field}>{labels[q.field]||q.field}：{q.question}</p>)}
          <p>可导入未确认报价后，通过现有字段修订填写补充值和来源理由；本次不把回答当审批。</p></div>:null}
        {run.can_import&&buyer?<>
          <label><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/>我理解这不是已确认事实，后续仍须逐字段核对</label>
          <label>采纳理由<input value={reason} maxLength={500} onChange={e=>setReason(e.target.value)} aria-label="候选采纳理由"/></label>
          <button type="button" disabled={busy||disabled||!ack||reason.trim().length<5} onClick={()=>void act(async()=>{
            await call(`/candidate-runs/${run.id}/import`,"POST",{expected_revision:run.revision,
              acknowledge_unverified_candidates:true,reason});if(active.current&&token.active)onImported();
          })}>导入为未确认报价</button>
        </>:null}
      </>:null}
    </article>)}
    <p className="muted">离开页面只停止浏览器等待，不表示服务端已取消，也不会自动重试已开始的模型调用。</p>
  </section>;
}
