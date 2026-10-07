"use client";
import type {Credential} from "@/lib/session.mjs";
import {useCallback,useEffect,useRef,useState,type FormEvent} from "react";
import {api,type PolicyVersion} from "@/lib/api";
import {APIError} from "@/lib/transport.mjs";

type Props={token:Credential;approver:boolean;workflowBusy:boolean;refreshEvent:number;onRead:(policy:PolicyVersion|null)=>void};
const statusLabels={effective:"当前生效",scheduled:"已发布，待生效",superseded:"历史版本"};
const when=(value:string)=>`${new Date(value).toLocaleString()} (${Intl.DateTimeFormat().resolvedOptions().timeZone})`;

export default function PolicyPanel({token,approver,workflowBusy,refreshEvent,onRead}:Props) {
  const [policy,setPolicy]=useState<PolicyVersion|null>(null);
  const [versions,setVersions]=useState<PolicyVersion[]>([]);
  const [loading,setLoading]=useState(true);
  const [readFailed,setReadFailed]=useState(false);
  const [working,setWorking]=useState(false);
  const [error,setError]=useState("");
  const [notice,setNotice]=useState("");
  const [form,setForm]=useState<PolicyVersion|null>(null);
  const [conflict,setConflict]=useState(false);
  const [scheduled,setScheduled]=useState(false);
  const [readAt,setReadAt]=useState("");
  const dialog=useRef<HTMLDialogElement|null>(null);
  const scope=useRef<{active:boolean;controller:AbortController}|null>(null);
  const epoch=useRef(0),lock=useRef(false),lastRefreshEvent=useRef(refreshEvent);
  const reload=useCallback(async()=>{
    const active=scope.current;if(!active?.active)return;
    const ticket=++epoch.current;setLoading(true);setError("");
    try {
      const [current,history]=await Promise.all([
        api<PolicyVersion>("/policy",token,"GET",undefined,active.controller.signal),
        api<PolicyVersion[]>("/policy/versions",token,"GET",undefined,active.controller.signal),
      ]);
      if(scope.current!==active||!active.active||ticket!==epoch.current)return;
      setPolicy(current);setVersions(history);setReadFailed(false);setReadAt(new Date().toLocaleTimeString());onRead(current);
      return current;
    } catch(e) {
      if(scope.current===active&&active.active&&ticket===epoch.current){
        setReadFailed(true);setError(`策略读取失败：${e instanceof Error?e.message:String(e)}。请刷新策略后再操作。`);onRead(null);
      }
    } finally {if(scope.current===active&&active.active&&ticket===epoch.current)setLoading(false);}
  },[token,onRead]);
  useEffect(()=>{
    const active={active:true,controller:new AbortController()};scope.current=active;void reload();
    return()=>{active.active=false;active.controller.abort();};
  },[reload]);
  useEffect(()=>{
    if(lastRefreshEvent.current===refreshEvent)return;
    lastRefreshEvent.current=refreshEvent;if(!lock.current)void reload();
  },[refreshEvent,reload]);
  useEffect(()=>{
    const upcoming=versions.filter(v=>v.status==="scheduled").map(v=>Date.parse(v.effective_at)).filter(Number.isFinite).sort((a,b)=>a-b)[0];
    if(!upcoming||loading)return;
    // Read-only refresh at the activation boundary; this never publishes a version.
    const timer=setTimeout(()=>void reload(),Math.min(2147483647,Math.max(250,upcoming-Date.now()+100)));
    return()=>clearTimeout(timer);
  },[versions,loading,reload]);
  useEffect(()=>{
    const focus=()=>{if(!lock.current)void reload();};window.addEventListener("focus",focus);
    return()=>window.removeEventListener("focus",focus);
  },[reload]);
  useEffect(()=>{if(form&&dialog.current&&!dialog.current.open)dialog.current.showModal();},[form]);
  const publish=async(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();if(!form||lock.current||workflowBusy||conflict||loading||readFailed)return;
    const values=new FormData(event.currentTarget),active=scope.current;
    if(!active?.active)return;
    const rawDate=String(values.get("effective_at")||"");
    const date=rawDate?new Date(rawDate):null;
    if(scheduled&&(!date||!Number.isFinite(date.getTime())||date.getTime()<=Date.now())){
      setError("请选择将来的生效时间；日期按当前浏览器时区解释。");return;
    }
    lock.current=true;setWorking(true);setError("");setNotice("");
    try {
      const saved=await api<PolicyVersion>("/policy/versions",token,"POST",{
        expected_version:form.latest_version,budget_cap:String(values.get("budget_cap")||"").trim()||null,
        max_delivery_days:values.get("max_delivery_days")===""?null:Number(values.get("max_delivery_days")),
        minimum_valid_quotes:Number(values.get("minimum_valid_quotes")),effective_at:scheduled?date!.toISOString():null,
        reason:String(values.get("reason")||"").trim(),
      },active.controller.signal);
      if(scope.current!==active||!active.active)return;
      setForm(null);setConflict(false);
      setNotice(`策略 v${saved.version} 已发布。${saved.status==="scheduled"?`将在 ${when(saved.effective_at)} 生效；当前采购仍使用已生效版本。`:"新校验使用此版本；旧方案、建议与审批须重新核验。"}`);
      await reload();
    } catch(e) {
      if(scope.current!==active||!active.active)return;
      if(e instanceof APIError&&e.status===409&&e.code==="VERSION_CONFLICT"){
        await reload();setConflict(true);setError("策略已被其他审批人更新。已回读最新历史；请先载入最新策略并重新核对，不能直接覆盖。不会自动重试发布。");
      } else {
        // A lost response may already have committed. Read history before any deliberate new publish.
        await reload();setConflict(true);
        setError(`${e instanceof Error?e.message:String(e)}。未自动重试。请核对版本历史确认是否已发布，再载入最新策略。`);
      }
    } finally {lock.current=false;if(scope.current===active&&active.active)setWorking(false);}
  };
  const openForm=()=>{if(!policy||readFailed)return;setForm(policy);setScheduled(false);setConflict(false);setError("");setNotice("");};
  const closeForm=()=>{setForm(null);setConflict(false);if(!readFailed)setError("");};
  return <section className="panel quote-panel policy-panel" data-testid="policy-panel" aria-labelledby="policy-title" aria-busy={loading||working}>
    <div className="section-head"><div><h2 id="policy-title">租户采购策略</h2><p>只追加版本，已发布记录不可编辑或删除。预算和交期始终采用需求与策略中较严格的限制。</p></div>
      <div className="heading-actions"><button className="secondary" data-testid="refresh-policy" disabled={loading||working||workflowBusy} onClick={()=>void reload()}>刷新策略</button>
        {/* A focus refresh must not disable this button between pointer-down and click.
            Editing keeps the captured version; publication still waits for the read and uses CAS. */}
        {approver&&<button className="primary" data-testid="new-policy" disabled={working||workflowBusy||!policy||readFailed||!!error} onClick={openForm}>发布新策略版本</button>}</div></div>
    <div className="decision-content">
      {!approver&&<p data-testid="policy-read-only">当前身份只读；只有独立审批人可发布新策略。</p>}
      {error&&<p role="alert" className="form-error" data-testid="policy-error">{error}</p>}
      {notice&&<p role="status" data-testid="policy-notice">{notice}</p>}
      {loading&&<p role="status">正在回读策略与变更历史…</p>}
      {policy&&<><div className="policy-summary" data-testid="effective-policy">
        <div><span className="badge ok">{error||loading?"上次回读生效":"当前生效"} v{policy.version}</span><p>预算上限：{policy.budget_cap===null?"未额外设限":`¥ ${policy.budget_cap}`}<br/>最长交期：{policy.max_delivery_days===null?"未额外设限":`${policy.max_delivery_days} 天`}<br/>最少合规供应商报价：{policy.minimum_valid_quotes} 家</p></div>
        <div><p>生效于 {when(policy.effective_at)}<br/>最近发布版本 v{policy.latest_version} · 回读于 {readAt}</p><div className="source-meta">策略哈希：{policy.policy_hash}</div></div>
      </div><p>空预算与空交期只表示策略未额外设限，需求本身的预算和交期仍然有效。未知字段不会计为零；只有合规且已确认的报价才计入最低数量，同一供应商的多份报价只计一次。</p></>}
      <details data-testid="policy-history"><summary>查看不可变更历史（{versions.length} 个版本）</summary>
        {versions.map(version=><article className="policy-version" data-testid="policy-version" key={version.version}>
          <div className="heading-actions"><strong>策略 v{version.version}</strong><span className={`badge ${version.status==="effective"?"ok":version.status==="scheduled"?"warn":""}`}>{statusLabels[version.status]}</span></div>
          <p>预算上限 {version.budget_cap===null?"未额外设限":`¥ ${version.budget_cap}`} · 交期上限 {version.max_delivery_days===null?"未额外设限":`${version.max_delivery_days} 天`} · 最少 {version.minimum_valid_quotes} 家合规供应商</p>
          <p data-testid="policy-reason">变更原因：{version.reason}</p><p>生效时间：{when(version.effective_at)}<br/>发布者：{version.created_by} · 发布时间：{when(version.created_at)}</p>
          <div className="source-meta">{version.id} · {version.policy_hash}</div>
        </article>)}
      </details>
    </div>
    {form&&<dialog ref={dialog} data-testid="policy-dialog" aria-labelledby="policy-form-title" onCancel={event=>{event.preventDefault();if(!working)closeForm();}}>
      <div className="modal-head"><h2 id="policy-form-title">发布策略 v{form.latest_version+1}</h2><button aria-label="关闭策略表单" disabled={working} onClick={closeForm}>×</button></div>
      <p>基于当前生效 v{form.version} 填写。新增版本保留全部历史；生效后旧评估、建议与审批将失效。尚未写入的 ERP 操作会再次检查策略；已完成或结果不确定的操作仅保留原快照并只读核对，不重放。</p>
      {loading&&<p role="status">正在回读最新策略，完成前不可发布。已填写内容与所依据的版本不会自动改变。</p>}
      {error&&<p role="alert" className="form-error">{error}</p>}
      {conflict&&<button className="secondary" data-testid="reload-policy-form" disabled={loading||working||!policy||readFailed} onClick={openForm}>载入最新策略并重新填写</button>}
      <form key={`${form.latest_version}:${form.policy_hash}`} data-testid="policy-form" onSubmit={publish}>
        <fieldset disabled={working||conflict} className="policy-fields"><div className="form-grid">
          <label className="form-field">预算上限（CNY，可留空）<input name="budget_cap" inputMode="decimal" defaultValue={form.budget_cap??""}/></label>
          <label className="form-field">最长交期（天，可留空）<input name="max_delivery_days" type="number" min="1" max="365" step="1" defaultValue={form.max_delivery_days??""}/></label>
          <label className="form-field">最少合规供应商报价数<input name="minimum_valid_quotes" type="number" min="1" max="100" step="1" defaultValue={form.minimum_valid_quotes} required/></label>
          <label className="form-field">生效方式<select name="timing" value={scheduled?"scheduled":"immediate"} onChange={e=>setScheduled(e.target.value==="scheduled")}><option value="immediate">立即生效</option><option value="scheduled">未来时间生效</option></select></label>
          {scheduled&&<label className="form-field full">生效时间（{Intl.DateTimeFormat().resolvedOptions().timeZone}）<input name="effective_at" type="datetime-local" required/><span>发布后仍使用当前生效版本，直到此时间到达。不能追溯生效。</span></label>}
          <label className="form-field full">变更原因<textarea name="reason" minLength={5} maxLength={500} required/></label>
        </div></fieldset>
        <div className="form-actions"><button type="button" className="secondary" disabled={working} onClick={closeForm}>取消</button><button className="primary" data-testid="publish-policy" disabled={working||conflict||loading||readFailed||workflowBusy}>{working?"发布中…":scheduled?"发布未来生效版本":"发布并立即生效"}</button></div>
      </form>
    </dialog>}
  </section>;
}
