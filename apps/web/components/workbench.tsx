"use client";
import {useCallback, useEffect, useRef, useState, type FormEvent} from "react";
import {API, api, loadDemo, watchAudit, type Identity, type Capabilities, type ProcurementRequest,
  type Quote, type QuoteValues, type EvidenceRef, type AuditEvent, type Operation, type AdviceRun, type DocumentEvidence} from "@/lib/api";

const names: Record<string,string> = {DRAFT:"待录入", NEEDS_CONFIRMATION:"待核对", READY_FOR_REVIEW:"待审批",
  APPROVED:"已批准", APPROVAL_STALE:"审批已失效", ERP_PENDING:"执行已预留", ERP_CREATED:"草稿已创建",
  RECONCILING:"待核对结果", NEEDS_HUMAN:"需要人工处理", BLOCKED:"规则未通过", REJECTED:"已拒绝"};
const labels: Record<keyof QuoteValues,string> = {supplier_id:"供应商编码", sku:"型号", quantity:"数量", uom:"单位",
  unit_price:"单价", tax_mode:"税价模式", tax_rate:"税率", shipping_cost:"最终含税运费", discount:"商品折扣额",
  delivery_days:"交期天数", currency:"币种"};
const fields = Object.keys(labels) as (keyof QuoteValues)[];
const requestFields = [["title","需求名称","研发工位支架采购"], ["sku","型号","STAND-01"],
  ["quantity","数量","20"], ["budget","预算","30000.00"], ["max_delivery_days","最长交期","14"]] as const;
type VerificationReceipt = {status:string; verified_at:string; remote_id:string|null; reason?:string; simulated:boolean};
const isFrozen = (r:ProcurementRequest|null) => !!r && ["ERP_PENDING","ERP_CREATED","RECONCILING","NEEDS_HUMAN"].includes(r.status);

const adviceStatus:Record<AdviceRun["status"],string> = {PENDING:"已预留，尚未调用模型",RUNNING:"生成中",COMPLETED:"已完成",
  FAILED:"生成失败",INTERRUPTED:"执行中断，禁止重放",STALE:"版本或来源已失效"};
type AdviceContext = {active:boolean;controller:AbortController};

/** This component is keyed by credential, request ID and version. Leaving its scope cancels
 * browser waits, never retries a provider call, and cannot publish a late result to a new scope. */
function AdvicePanel({request,token,cap,buyer,workflowBusy,quotes,freshnessEvent,beginEvidence,onEvidence}:{request:ProcurementRequest;token:string;
  cap:Capabilities|null;buyer:boolean;workflowBusy:boolean;quotes:Quote[];beginEvidence:()=>number;
  freshnessEvent:number;onEvidence:(evidence:EvidenceRef,ticket:number)=>void}) {
  const [runs,setRuns] = useState<AdviceRun[]>([]);
  const [loading,setLoading] = useState(true);
  const [readError,setReadError] = useState("");
  const [runError,setRunError] = useState("");
  const [working,setWorking] = useState(false);
  const [citationBusy,setCitationBusy] = useState(false);
  const [citationError,setCitationError] = useState("");
  const [processingRunId,setProcessingRunId] = useState<string|null>(null);
  const context = useRef<AdviceContext|null>(null);
  const mutationLock = useRef(false);
  const processingNetworkPending = useRef(false);
  const lastFreshnessEvent = useRef(freshnessEvent);
  const readEpoch = useRef(0);
  const scopeIsActive = (scope:AdviceContext) => scope.active && context.current === scope;
  const reload = useCallback(async(scope:AdviceContext) => {
    const epoch = ++readEpoch.current;
    if (scopeIsActive(scope)) {setLoading(true);setReadError("");}
    try {
      const saved = await api<AdviceRun[]>(`/requests/${request.id}/advice-runs`,token,"GET",undefined,scope.controller.signal);
      if (scopeIsActive(scope) && epoch === readEpoch.current) {
        setRuns(saved);
        if (!processingNetworkPending.current)
          setProcessingRunId(id=>saved.some(run=>run.id === id && run.status !== "PENDING") ? null : id);
      }
    } catch (error) {
      if (scopeIsActive(scope) && epoch === readEpoch.current)
        setReadError(error instanceof Error ? error.message : String(error));
    } finally {
      if (scopeIsActive(scope) && epoch === readEpoch.current) setLoading(false);
    }
  },[request.id,token]);
  useEffect(()=>{
    const scope = {active:true,controller:new AbortController()};context.current = scope;
    void reload(scope);
    return ()=>{scope.active=false;scope.controller.abort();};
  },[reload]);
  useEffect(()=>{
    if (lastFreshnessEvent.current === freshnessEvent) return;
    lastFreshnessEvent.current=freshnessEvent;
    if (context.current) void reload(context.current);
  },[freshnessEvent,reload]);
  // Poll persisted state only. Mount, refresh and polling must never POST /process.
  useEffect(()=>{
    if (!runs.some(run=>run.status === "RUNNING") || working || readError) return;
    const timer = setTimeout(()=>{if (context.current) void reload(context.current);},2000);
    return ()=>clearTimeout(timer);
  },[runs,working,readError,reload]);
  const retain = (scope:AdviceContext,run:AdviceRun) => {
    if (!scopeIsActive(scope)) return;
    ++readEpoch.current;setLoading(false);
    setRuns(old=>[run,...old.filter(item=>item.id !== run.id)].slice(0,20));
  };
  const start = async(pending?:AdviceRun) => {
    const scope=context.current;
    if (!scope || !scopeIsActive(scope) || mutationLock.current || !buyer || !cap?.advice_configured || workflowBusy) return;
    mutationLock.current=true;setWorking(true);setRunError("");
    try {
      const run = pending || await api<AdviceRun>(`/requests/${request.id}/advice-runs`,token,"POST",
        {expected_version:request.version,idempotency_key:crypto.randomUUID()},scope.controller.signal);
      if (!scopeIsActive(scope)) return; // Never start paid work for an abandoned reservation response.
      retain(scope,run);
      if (run.status === "PENDING" && run.current && run.request_version === request.version) {
        setProcessingRunId(run.id);processingNetworkPending.current=true;
        const result = await api<AdviceRun>(`/advice-runs/${run.id}/process`,token,"POST",undefined,scope.controller.signal);
        retain(scope,result);
        if (scopeIsActive(scope)) setProcessingRunId(null);
        if (scopeIsActive(scope)) await reload(scope);
      }
    } catch (error) {
      processingNetworkPending.current=false;
      if (scopeIsActive(scope)) {
        setRunError(`${error instanceof Error ? error.message : String(error)}。未自动重试；请回读状态。失败或中断后须显式新建运行。`);
        await reload(scope); // A lost response may still have a persisted terminal result.
      }
    } finally {
      processingNetworkPending.current=false;
      mutationLock.current=false;
      if (scopeIsActive(scope)) setWorking(false);
    }
  };
  const isCurrent = (run:AdviceRun) => run.current && run.request_version === request.version;
  const activeRun = runs.some(run=>isCurrent(run) && ["PENDING","RUNNING"].includes(run.status));
  const citationDocument = (id:string) => quotes.find(quote=>id.startsWith(quote.document_id+":"));
  const openCitation = async(id:string) => {
    const scope=context.current,quote=citationDocument(id);
    if (!scope || !scopeIsActive(scope) || !quote || citationBusy) return;
    const ticket=beginEvidence();setCitationBusy(true);setCitationError("");
    try {
      const source = await api<DocumentEvidence>(`/documents/${quote.document_id}/evidence`,token,"GET",undefined,scope.controller.signal);
      const fragment=source.fragments.find(item=>item.id === id);
      const knownHash=Object.values(quote.evidence).find(item=>item.document_sha256)?.document_sha256;
      if (source.id !== quote.document_id || !fragment || (knownHash && knownHash !== source.sha256))
        throw new Error("SOURCE_INTEGRITY_FAILED：引用片段缺失或来源哈希不匹配");
      if (scopeIsActive(scope)) onEvidence({...fragment.locator,fragment_id:id,text:fragment.text,document_sha256:source.sha256},ticket);
    } catch (error) {
      if (scopeIsActive(scope)) setCitationError(`${error instanceof Error ? error.message : String(error)}。无法核验此来源，请回读状态或检查原始文件。`);
    } finally {if (scopeIsActive(scope)) setCitationBusy(false);}
  };
  return <section className="panel quote-panel" data-testid="advice-panel" aria-labelledby="advice-title" aria-busy={loading||working}>
    <div className="section-head"><div><h2 id="advice-title">Agent 只读建议</h2>
      <p>绑定需求 v{request.version} 与来源快照。建议不会审批、采购、生成 ERP 草稿或改变确定性规则结果。</p></div>
      <div className="heading-actions"><button className="secondary" data-testid="refresh-advice" disabled={loading||working}
        onClick={()=>{if (context.current) void reload(context.current);}}>回读建议状态</button>
        {buyer && <button className="primary" data-testid="new-advice" disabled={workflowBusy||working||loading||!!readError||!cap?.advice_configured||activeRun}
          onClick={()=>void start()}>{working ? "正在生成只读建议…" : "新建只读建议"}</button>}</div>
    </div>
    <div className="decision-content">
      <p data-testid="advice-provider">{cap?.advice_configured
        ? "模型提供方已配置：仅检测到服务端密钥与模型设置，尚未验证连通性或质量。显式生成可能产生模型费用。"
        : "模型提供方未配置：请在服务端设置 LLM_API_KEY 与 LLM_MODEL；当前不能生成建议。"}</p>
      <p>运行方式：{cap?.advice_runtime||"未报告"}。仅供参考，必须人工核对。引用 ID 存在校验不代表语义真实性验证。</p>
      {!buyer && <p data-testid="advice-read-only">当前身份只能查看历史；仅采购员可发起运行。</p>}
      {readError && <p role="alert" className="form-error" data-testid="advice-read-error">历史读取失败：{readError}。请回读状态后再操作。</p>}
      {runError && <p role="alert" className="form-error" data-testid="advice-error">{runError}</p>}
      {citationError && <p role="alert" className="form-error" data-testid="advice-citation-error">{citationError}</p>}
      {loading && <p role="status">正在回读已保存的运行…</p>}
      {!loading && !readError && !runs.length && <p className="subtle-empty" data-testid="advice-empty">尚无已保存建议。只有显式发起才会调用模型。</p>}
      <div data-testid="advice-history">{runs.map(run=><article key={run.id} data-testid="advice-run" style={{borderTop:"1px solid var(--line)",paddingTop:16,marginTop:16}}>
        <div className="heading-actions"><strong data-testid="advice-status">{run.status === "PENDING" && processingRunId === run.id
          ? "处理请求已发送，模型调用可能进行中" : adviceStatus[run.status]}</strong>
          <span className="badge">需求 v{run.request_version}</span>
          <span className={`badge ${isCurrent(run) ? "ok" : "warn"}`} data-testid="advice-current">{isCurrent(run) ? "当前版本及来源" : "历史结果，已失效"}</span></div>
        <p className="source-meta">运行 {run.id} · 创建于 {run.created_at}{run.completed_at ? ` · 结束于 ${run.completed_at}` : ""}</p>
        {!isCurrent(run) && <p data-testid="advice-stale">需求版本或证据来源已变化或无法核验，不能将旧建议视为当前结论。{run.stale_reason||"REQUEST_VERSION_CHANGED"}</p>}
        {run.error_code && <p className="form-error" data-testid="advice-failure">{run.error_code}。本次运行不会自动重试；修正来源或配置后显式新建运行。</p>}
        {run.status === "INTERRUPTED" && <p>调用结果不确定，可能已产生费用。此运行不能重放；如需再次调用，请新建运行。</p>}
        {run.status === "RUNNING" && <p>正在回读运行状态，页面刷新不会重放模型调用。</p>}
        {run.status === "PENDING" && <p>{processingRunId === run.id
          ? "回读结果前不能确认是否已经调用模型，可能产生费用。不会自动重试。"
          : "仅已预留，尚未执行。刷新或切换回来不会自动调用模型。"}</p>}
        {buyer && run.status === "PENDING" && isCurrent(run) && <button className="secondary" data-testid="process-advice"
          disabled={workflowBusy||working||loading||!!readError||!cap?.advice_configured} onClick={()=>void start(run)}>
          {processingRunId === run.id ? "重新提交此运行的处理请求" : "执行此已预留运行"}</button>}
        {run.output && <div data-testid="advice-output">
          <p style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere",marginTop:12}}>{run.output.summary}</p>
          <p>仅供参考 · 语义真实性未验证 · {run.output.evidence_read_verified ? "引用片段存在且已在运行中读取" : "引用只校验来源 ID 存在"}</p>
          <div data-testid="advice-citations" className="source-meta">引用来源：{run.output.evidence_ids.length ? run.output.evidence_ids.map(id=><div key={id}>
            {citationDocument(id) ? <button className="field-link" data-testid="advice-citation" disabled={citationBusy||workflowBusy}
              style={{textAlign:"left",overflowWrap:"anywhere"}} onClick={()=>void openCitation(id)}>核验并查看 {id}</button>
              : <span>{id}（不在当前报价来源中，无法打开）</span>}</div>) : "未引用来源片段"}</div>
          <p>模型调用 {run.output.model_calls} 次 · 只读工具调用 {run.output.tool_calls} 次 · {run.output.usage_complete && run.output.usage
            ? `提供方报告 token 总量 ${run.output.usage.total_tokens}` : "token 用量不完整或未知"} · 费用未知</p>
          <details><summary>查看只读运行记录</summary><pre>{JSON.stringify({runtime:run.output.runtime,llm_used:run.output.llm_used,
            advisory_only:run.output.advisory_only,evidence_read_verified:run.output.evidence_read_verified,
            semantic_factuality_verified:run.output.semantic_factuality_verified,
            usage:run.output.usage,usage_complete:run.output.usage_complete,trace:run.output.trace},null,2)}</pre></details>
        </div>}
        <details><summary>查看输入快照标识</summary><div className="hash">{run.input_hash}</div></details>
      </article>)}</div>
      {!!runs.length && <p style={{marginTop:14}}>最多显示最近 20 次持久化运行。失败、中断或失效均不会触发自动重试。</p>}
    </div>
  </section>;
}

export default function Workbench() {
  const [token,setToken] = useState("");
  const [me,setMe] = useState<Identity|null>(null);
  const [cap,setCap] = useState<Capabilities|null>(null);
  const [requests,setRequests] = useState<ProcurementRequest[]>([]);
  const [selected,setSelected] = useState<ProcurementRequest|null>(null);
  const [quotes,setQuotes] = useState<Quote[]>([]);
  const [events,setEvents] = useState<AuditEvent[]>([]);
  const [evidence,setEvidence] = useState<EvidenceRef|null>(null);
  const [editing,setEditing] = useState<Quote|null>(null);
  const [confirming,setConfirming] = useState<Quote|null>(null);
  const [requestForm,setRequestForm] = useState<ProcurementRequest|"new"|null>(null);
  const [operation,setOperation] = useState<Operation|null>(null);
  const [verification,setVerification] = useState<VerificationReceipt|null>(null);
  const [error,setError] = useState("");
  const [busy,setBusy] = useState(false);
  const [stream,setStream] = useState("idle");
  const uploadRef = useRef<HTMLInputElement>(null);
  const mutationLock = useRef(false);
  const readEpoch = useRef(0);
  const activeScope = useRef("");
  const evidenceReadEpoch = useRef(0);
  const buyer = me?.role === "buyer";
  const frozen = isFrozen(selected);

  const act = useCallback(async(fn:()=>Promise<void>) => {
    if (mutationLock.current) return;
    mutationLock.current = true; setBusy(true); setError("");
    try { await fn(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { mutationLock.current = false; setBusy(false); }
  },[]);
  const clearContext = useCallback(() => {
    ++evidenceReadEpoch.current;
    setSelected(null); setQuotes([]); setEvents([]); setOperation(null); setEvidence(null); setVerification(null);
    setEditing(null); setConfirming(null); setRequestForm(null); setStream("idle");
  },[]);
  const refresh = useCallback(async(id:string, credential=token) => {
    ++evidenceReadEpoch.current;
    const epoch = ++readEpoch.current;
    activeScope.current = credential + "\n" + id;
    setEvidence(null); setEditing(null); setConfirming(null); setVerification(null);
    const [r,q,e,list] = await Promise.all([
      api<ProcurementRequest>(`/requests/${id}`,credential), api<Quote[]>(`/requests/${id}/quotes`,credential),
      api<AuditEvent[]>(`/requests/${id}/events`,credential), api<ProcurementRequest[]>("/requests",credential),
    ]);
    const reserved = [...e].reverse().find(x=>x.type === "ERP_OPERATION_RESERVED");
    const op = reserved ? await api<Operation>(`/operations/${reserved.payload.operation_id}`,credential) : null;
    if (epoch !== readEpoch.current) return; // A slower response must not overwrite a newer identity/request.
    setSelected(r); setQuotes(q); setEvents(e); setRequests(list); setOperation(op);
  },[token]);
  const login = async(credential:string) => {
    const epoch = ++readEpoch.current;
    activeScope.current = ""; setToken(""); setMe(null); setCap(null); setRequests([]); clearContext();
    const [identity,capabilities,list] = await Promise.all([
      api<Identity>("/me",credential), api<Capabilities>("/capabilities",credential), api<ProcurementRequest[]>("/requests",credential),
    ]);
    if (epoch !== readEpoch.current) return;
    setToken(credential); setMe(identity); setCap(capabilities); setRequests(list);
    if (list.length) await refresh(list[0].id,credential);
  };
  const choose = async(id:string) => { clearContext(); await refresh(id); };
  useEffect(() => {
    if (!selected || !token) return;
    const controller = new AbortController();
    const scope = token + "\n" + selected.id;
    void watchAudit(API,token,selected.id,{
      signal:controller.signal,
      onStatus:value=>{if (!controller.signal.aborted && activeScope.current === scope) setStream(value);},
      onEvent:event=>{
        if (!controller.signal.aborted && activeScope.current === scope)
          setEvents(old=>old.some(e=>e.id === event.id) ? old : [...old,event]);
      },
    });
    return ()=>controller.abort();
  },[selected?.id,token]);

  const saveRequest = async(event:FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget), target = requestForm;
    await act(async()=>{
      const body = {title:form.get("title"), sku:form.get("sku"), quantity:form.get("quantity"),
        budget:form.get("budget"), max_delivery_days:Number(form.get("max_delivery_days")), uom:"EA", currency:"CNY"};
      const r = target && target !== "new"
        ? await api<ProcurementRequest>(`/requests/${target.id}`,token,"PUT",{...body,expected_version:target.version})
        : await api<ProcurementRequest>("/requests",token,"POST",body);
      setRequestForm(null); await refresh(r.id);
    });
  };
  const saveQuote = async(event:FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!editing) return;
    const q = editing, form = new FormData(event.currentTarget);
    const values = Object.fromEntries(fields.map(key=>[key,form.get(key) === "" ? null : key === "delivery_days" ? Number(form.get(key)) : form.get(key)]));
    await act(async()=>{
      await api(`/quotes/${q.id}`,token,"PUT",{expected_version:q.version,values,reason:form.get("reason")});
      setEditing(null); await refresh(q.request_id);
    });
  };
  const confirm = async(event:FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!confirming) return;
    const q = confirming;
    if (new FormData(event.currentTarget).get("acknowledge") !== "on") return;
    await act(async()=>{
      await api(`/quotes/${q.id}/confirm`,token,"POST",{expected_version:q.version,acknowledge:true});
      setConfirming(null); await refresh(q.request_id);
    });
  };
  const demo = () => act(async()=>{
    let created:string|null = null;
    try { await loadDemo(API,token,{onCreated:id=>{created=id;}}); }
    finally { if (created) await refresh(created); }
  });
  const approve = (decision:"approve"|"reject") => act(async()=>{
    if (!selected?.proposal) return;
    await api(`/requests/${selected.id}/approval`,token,"POST",{snapshot_hash:selected.proposal.snapshot_hash,decision});
    await refresh(selected.id);
  });
  const execute = () => act(async()=>{
    if (!selected?.proposal) return;
    const op = await api<Operation>(`/requests/${selected.id}/execute`,token,"POST",{snapshot_hash:selected.proposal.snapshot_hash});
    setOperation(op);
    try { await api(`/operations/${op.id}/process`,token,"POST"); }
    finally { await refresh(selected.id); }
  });

  return <>
    <aside className="sidebar">
      <a className="brand" href="/"><span className="logo">P</span><span>ProcureFlow<small>EVIDENCE BEFORE ACTION</small></span></a>
      <div className="workspace">Next.js 原生工作台 <span className="tag">ALPHA 2</span></div>
      <nav><button className="nav-active">▦ 采购工作台</button></nav><div className="sidebar-label">采购需求</div>
      <div id="request-list">{requests.map(r=><button key={r.id} disabled={busy} className={selected?.id === r.id ? "selected" : ""}
        onClick={()=>void act(()=>choose(r.id))}>{r.title}<small>{names[r.status]||r.status}</small></button>)}</div>
      <div className="side-bottom">{cap?.erp_mode === "erpnext" ? "ERPNext · 授权沙箱草稿" : "模拟 ERP · 规则基线"}<br/>禁止自动审批及正式提交</div>
    </aside>
    <main>
      <header className="topbar"><span>工作区 / 采购中心</span>
        <form onSubmit={e=>{e.preventDefault();const value=String(new FormData(e.currentTarget).get("token"));void act(()=>login(value));}} className="identity">
          <input aria-label="访问令牌" name="token" type="password" autoComplete="off" placeholder="服务端分配的访问令牌" required/>
          <button className="secondary" disabled={busy}>登录</button><span data-testid="identity">{me?.role||"未登录"}</span>
        </form>
        {cap?.demo_samples && <select aria-label="切换演示身份" disabled={busy} value={token} onChange={e=>void act(()=>login(e.target.value))}>
          <option value="demo-buyer">采购员</option><option value="demo-approver">独立审批人</option><option value="demo-auditor">审计员</option>
          {!token.startsWith("demo-") && <option value={token}>当前自定义身份</option>}
        </select>}
      </header>
      <section className="content" aria-busy={busy}>
        <div className="page-heading"><div><div className="eyebrow">PROCUREMENT / WORKBENCH</div>
          <h1>让每一笔采购，都有据可依。</h1><p>字段证据、确定性金额、版本绑定审批与受控执行。</p></div>
          <div className="heading-actions">{cap?.demo_samples && <button data-testid="load-demo" className="secondary" disabled={!buyer||busy} onClick={()=>void demo()}>载入三份示例</button>}
            <button className="primary" disabled={!buyer||busy} onClick={()=>setRequestForm("new")}>＋ 新建采购需求</button></div>
        </div>
        <div className="notice">本地 Alpha：金额由确定性规则计算，不是 LLM 推理。当前 ERP：{cap?.erp_mode||"尚未认证"}。正式提交始终不在本工作台权限内。</div>
        {error && <div role="alert" className="form-error">{error}</div>}
        <div className="metrics">{[["采购需求",requests.length],["报价版本",quotes.length],["已确认",quotes.filter(q=>q.confirmed_by).length],["审计记录",events.length]].map(([label,count])=>
          <div className="metric" key={label}><label>{label}</label><strong>{count}</strong></div>)}</div>
        {!selected ? <section className="empty panel"><h2>{me ? "创建或选择一项采购需求" : "登录后开始采购核对"}</h2>
          <p>演示环境采用合成数据与独立身份，不会写入真实 ERP；已配置模型仅在显式生成只读建议时调用。</p>
          {!me && <button data-testid="login-demo-buyer" className="primary" disabled={busy} onClick={()=>void act(()=>login("demo-buyer"))}>以演示采购员登录</button>}
        </section> : <>
          <section className="panel request-panel"><div><div className="eyebrow">{selected.id}</div><h2>{selected.title}</h2>
            <p data-testid="request-meta">{selected.sku} · {selected.quantity} {selected.uom} · 预算 ¥{selected.budget} · ≤ {selected.max_delivery_days} 天 · v{selected.version}</p></div>
            <div className="heading-actions"><span className="badge" data-testid="request-status">{names[selected.status]||selected.status}</span>
              <button className="secondary" disabled={busy} onClick={()=>void act(()=>refresh(selected.id))}>刷新状态</button>
              <button className="secondary" data-testid="edit-request" disabled={!buyer||frozen||busy} onClick={()=>setRequestForm(selected)}>修改需求</button>
            </div>
          </section>
          <section className="panel quote-panel"><div className="section-head"><div><h2>供应商报价比较</h2><p>未知不是零。点击数值查看字段来源；确认不会补全缺失值。</p></div>
            <div className="heading-actions"><input ref={uploadRef} type="file" hidden accept=".txt,.csv,.pdf,.xlsx" onChange={e=>{
              const file=e.target.files?.[0]; e.target.value="";
              if (file) void act(async()=>{const form=new FormData();form.append("file",file);await api(`/requests/${selected.id}/documents`,token,"POST",form);await refresh(selected.id);});
            }}/><button className="secondary" disabled={!buyer||frozen||busy} onClick={()=>uploadRef.current?.click()}>上传报价</button>
              <button className="primary" data-testid="analyze" disabled={!buyer||frozen||busy} onClick={()=>void act(async()=>{await api(`/requests/${selected.id}/analyze`,token,"POST",{});await refresh(selected.id);})}>校验并生成方案</button>
            </div></div>
            <div className="table-scroll"><table><thead><tr><th>供应商</th><th>单价</th><th>税价</th><th>运费</th><th>统一总价</th><th>核对状态</th><th>操作</th></tr></thead>
              <tbody data-testid="quote-body">{quotes.map(q=><tr key={q.id} data-testid="quote-row"><td><strong>{q.values.supplier_id||"未知供应商"}</strong><small>{q.filename} · v{q.version}</small></td>
                {(["unit_price","tax_mode","shipping_cost"] as const).map(key=><td key={key}><button className="field-link" data-testid={`field-${key}`} onClick={()=>{++evidenceReadEpoch.current;setEvidence(q.evidence[key]||{kind:"unknown"});}}>{q.values[key]??"未知"}</button></td>)}
                <td><strong>{q.calculation.total??"未知"}</strong></td><td><span className="badge">{q.confirmed_by?"已确认":"待核对"}</span>
                  <small>{q.calculation.violations.join(" / ")}</small></td><td><div className="row-actions">
                    <button className="secondary" disabled={!buyer||frozen||busy} onClick={()=>setEditing(q)}>修正字段</button>
                    <button className="secondary" data-testid="confirm-quote" disabled={!buyer||frozen||busy||!!q.confirmed_by} onClick={()=>setConfirming(q)}>确认字段</button>
                  </div></td></tr>)}</tbody></table></div>
          </section>
          <div className="lower-grid"><section className="panel"><div className="section-head"><h2>字段证据</h2></div><div id="evidence-body" className="evidence-body" data-testid="evidence-body">
            {evidence ? <><pre>{evidence.text||evidence.reason||"该字段没有原文支持，保持未知。"}</pre>
              <div className="source-meta">{evidence.page?`第 ${evidence.page} 页 / 行 ${evidence.line}`:evidence.cell_range?`${evidence.sheet}!${evidence.cell_range}`:evidence.line?`文本第 ${evidence.line} 行`:evidence.kind}<br/>{evidence.document_sha256}</div></>
              : <p className="subtle-empty">选择一项报价字段</p>}
          </div></section>
          <section className="panel"><div className="section-head"><h2>方案与审批</h2></div><div className="decision-content" data-testid="proposal-body">
            {selected.proposal ? <><div className="decision-label">{selected.proposal.quote_values.supplier_id}</div><div className="decision-amount">¥ {selected.proposal.total}</div>
              <p>报价版本 v{selected.proposal.quote_version} · 数量 {selected.proposal.quote_values.quantity} · {selected.proposal.quote_values.uom}</p>
              <div className="hash">{selected.proposal.snapshot_hash}</div>
              {me?.role === "approver" && !frozen && <div className="heading-actions">
                <button className="primary" data-testid="approve" disabled={busy} onClick={()=>void approve("approve")}>批准展示的快照</button>
                <button className="secondary" data-testid="reject" disabled={busy} onClick={()=>void approve("reject")}>拒绝此方案</button></div>}
              {buyer && selected.status === "APPROVED" && <button className="primary" data-testid="execute" disabled={busy||!cap?.erp_draft_writes_enabled} onClick={()=>void execute()}>
                {cap?.erp_mode === "mock" ? "创建模拟 ERP 草稿" : "创建已批准的 ERPNext 草稿"}</button>}
              {operation && <><button className="secondary" data-testid="verify-erp" disabled={busy} onClick={()=>void act(async()=>{
                const scope = activeScope.current;
                const receipt = await api<VerificationReceipt>(`/operations/${operation.id}/verify`,token,"POST");
                if (activeScope.current === scope) setVerification(receipt);
              })}>独立回读核对 ERP 草稿</button>
                {verification && <div data-testid="erp-verification" role="status"><strong>{verification.status === "verified" ? "草稿与审批快照一致" : "核对未通过：" + verification.status}</strong>
                  <p>{verification.simulated ? "模拟 ERP" : "ERPNext"} · {verification.verified_at} · {verification.remote_id||verification.reason}</p>
                  <small>本次仅回读，不创建、不重试、不改写历史执行状态。</small></div>}
                <p className="decision-result">{operation.status} · {operation.remote_id||operation.error}</p>
                {buyer && operation.status !== "COMPLETED" && <button className="secondary" disabled={busy} onClick={()=>void act(async()=>{
                  await api(`/operations/${operation.id}/process`,token,"POST"); await refresh(selected.id);
                })}>处理 / 只读核对</button>}</>}
            </> : <p className="subtle-empty">核对报价后生成方案；需求或报价变化后需要重新生成和审批。</p>}
          </div></section></div>
          <AdvicePanel key={token+"\n"+selected.id+"\n"+selected.version} request={selected} token={token} cap={cap} buyer={buyer} workflowBusy={busy}
            quotes={quotes} freshnessEvent={events.reduce((latest,event)=>["REQUEST_CHANGED","QUOTE_IMPORTED","QUOTE_CONFIRMED","QUOTE_VERSION_CREATED"].includes(event.type) ? Math.max(latest,event.id) : latest,0)}
            beginEvidence={()=>{setEvidence(null);return ++evidenceReadEpoch.current;}} onEvidence={(source,ticket)=>{
              if (ticket !== evidenceReadEpoch.current) return;
              setEvidence(source);document.getElementById("evidence-body")?.scrollIntoView({block:"center",behavior:"smooth"});
            }}/>
          <section className="panel quote-panel"><div className="section-head"><h2>审计时间线</h2><span className="live" data-testid="stream-status">SSE · {stream}</span></div>
            <div id="events">{[...events].sort((a,b)=>b.id-a.id).map(e=><div className="event" key={e.id}><time>{e.created_at.slice(11,19)}</time>
              <div><strong>{e.type}</strong><p>{e.actor_id} · {JSON.stringify(e.payload)}</p></div></div>)}</div>
          </section>
        </>}
        <footer>ProcureFlow v0.1.0a2<span>Next.js 源码 / 非生产系统</span></footer>
      </section>
    </main>
    {requestForm && <dialog open aria-labelledby="request-form-title"><div className="modal-head"><h2 id="request-form-title">{requestForm === "new" ? "创建采购需求" : "修改需求：旧审批将失效"}</h2>
      <button aria-label="关闭需求表单" disabled={busy} onClick={()=>setRequestForm(null)}>×</button></div>
      <form data-testid="request-form" onSubmit={saveRequest}><div className="form-grid">{requestFields.map(([name,label,value])=><label className="form-field" key={name}>{label}
        <input name={name} defaultValue={requestForm === "new" ? value : String(requestForm[name])} required/></label>)}</div>
        <div className="form-actions"><button type="submit" className="primary" disabled={busy}>保存需求</button></div></form></dialog>}
    {editing && <dialog open aria-labelledby="quote-form-title"><div className="modal-head"><h2 id="quote-form-title">报价 v{editing.version} · 修改后需重新确认</h2>
      <button aria-label="关闭报价表单" disabled={busy} onClick={()=>setEditing(null)}>×</button></div><form onSubmit={saveQuote}><div className="form-grid">
        {fields.map(key=><label className="form-field" key={key}>{labels[key]}{key === "tax_mode" ? <select name={key} defaultValue={editing.values[key]}>
          <option value="included">含税</option><option value="excluded">未税</option><option value="unknown">未知</option></select>
          : <input name={key} defaultValue={editing.values[key]??""}/>}</label>)}
        <label className="form-field full">修改依据<textarea name="reason" minLength={5} maxLength={500} required/></label></div>
        <div className="form-actions"><button className="primary" disabled={busy}>保存新版本</button></div></form></dialog>}
    {confirming && <dialog open aria-labelledby="confirm-title"><div className="modal-head"><h2 id="confirm-title">核对 {confirming.values.supplier_id} · v{confirming.version}</h2>
      <button aria-label="关闭确认表单" disabled={busy} onClick={()=>setConfirming(null)}>×</button></div>
      <form onSubmit={confirm}><p>请逐项检查原始报价。确认只记录核对人，不会把未知运费、税率等变成零。</p>
        <div className="confirm-values">{fields.map(key=><div key={key}><strong>{labels[key]}：</strong>{confirming.values[key]??"未知"}</div>)}</div>
        <label className="ack-label"><input type="checkbox" name="acknowledge" required data-testid="ack-confirm"/>我已核对展示的报价版本，保留所有未知字段。</label>
        <div className="form-actions"><button type="submit" className="primary" data-testid="submit-confirm" disabled={busy}>确认此版本</button></div>
      </form></dialog>}
  </>;
}
