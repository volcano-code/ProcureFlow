"use client";
import {Fragment, useCallback, useEffect, useRef, useState, type FormEvent} from "react";
import {API, api, loadDemo, watchAudit, type Capabilities, type ProcurementRequest,
  type Quote, type EvidenceRef, type AuditEvent, type Operation, type AdviceRun, type DocumentEvidence, type PolicyVersion} from "@/lib/api";
import SessionBoundary,{type AuthenticatedProps} from "@/components/session-boundary";
import type {Credential} from "@/lib/session.mjs";
import PolicyPanel from "@/components/policy-panel";
import EvaluationPanel from "@/components/evaluation-panel";
import TableImportButton from "@/components/table-import-dialog";
import RecoveryPanel from "@/components/recovery-panel";
import {RequestEditor,QuoteEditor} from "@/components/procurement-editors";
import QuoteLineDetails from "@/components/quote-line-details";
import {FIELD_LABELS,HEADER_FIELDS} from "@/lib/procurement-lines.mjs";
import {bindingCurrent,staleExplanation,strictestBudget,violationExplanation} from "@/lib/policy.mjs";
import {ADVICE_WRITE_TIMEOUT_MS,AdviceWaitError,adviceFailureExplanation,providerAttemptExplanation,waitForAdvice} from "@/lib/advice.mjs";

const names: Record<string,string> = {DRAFT:"待录入", NEEDS_CONFIRMATION:"待核对", READY_FOR_REVIEW:"待审批",
  APPROVED:"已批准", APPROVAL_STALE:"审批已失效", ERP_PENDING:"执行已预留", ERP_CREATED:"草稿已创建",
  RECONCILING:"待核对结果", NEEDS_HUMAN:"需要人工处理", BLOCKED:"规则未通过", REJECTED:"已拒绝"};
const labels = FIELD_LABELS;
const fields = Object.keys(labels) as (keyof typeof labels)[];
type VerificationReceipt = {status:string; verified_at:string; remote_id:string|null; reason?:string; simulated:boolean};
const isFrozen = (r:ProcurementRequest|null) => !!r && ["ERP_PENDING","ERP_CREATED","RECONCILING","NEEDS_HUMAN"].includes(r.status);

const adviceStatus:Record<AdviceRun["status"],string> = {PENDING:"已预留，尚未调用模型",RUNNING:"生成中",COMPLETED:"已完成",
  FAILED:"生成失败",INTERRUPTED:"执行中断，禁止重放",STALE:"版本或来源已失效"};
type AdviceContext = {active:boolean;controller:AbortController};

/** This component is keyed by session generation, request ID and version. Leaving its scope cancels
 * browser waits, never retries a provider call, and cannot publish a late result to a new scope. */
function AdvicePanel({request,token,cap,buyer,workflowBusy,quotes,freshnessEvent,policyHash,beginEvidence,onEvidence}:{request:ProcurementRequest;token:Credential;
  cap:Capabilities|null;buyer:boolean;workflowBusy:boolean;quotes:Quote[];beginEvidence:()=>number;
  freshnessEvent:string;policyHash:string|null;onEvidence:(evidence:EvidenceRef,ticket:number)=>void}) {
  const [runs,setRuns] = useState<AdviceRun[]>([]);
  const [loading,setLoading] = useState(true);
  const [readError,setReadError] = useState("");
  const [runError,setRunError] = useState("");
  const [working,setWorking] = useState(false);
  const [citationBusy,setCitationBusy] = useState(false);
  const [citationError,setCitationError] = useState("");
  const [processingRunId,setProcessingRunId] = useState<string|null>(null);
  const [verifiedFreshness,setVerifiedFreshness] = useState<string|null>(null);
  const context = useRef<AdviceContext|null>(null);
  const mutationLock = useRef(false);
  const mutationController = useRef<AbortController|null>(null);
  const processingNetworkPending = useRef(false);
  const lastFreshnessEvent = useRef(freshnessEvent);
  const latestFreshnessEvent = useRef(freshnessEvent);
  latestFreshnessEvent.current = freshnessEvent;
  const readEpoch = useRef(0);
  const scopeIsActive = (scope:AdviceContext) => scope.active && context.current === scope;
  const reload = useCallback(async(scope:AdviceContext) => {
    const epoch = ++readEpoch.current;
    const freshness = latestFreshnessEvent.current;
    if (scopeIsActive(scope)) {setLoading(true);setReadError("");}
    try {
      const saved = await waitForAdvice(signal=>api<AdviceRun[]>(`/requests/${request.id}/advice-runs`,token,"GET",undefined,signal),
        {signal:scope.controller.signal});
      if (scopeIsActive(scope) && epoch === readEpoch.current) {
        setRuns(saved);
        setVerifiedFreshness(freshness);
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
    mutationController.current?.abort(new AdviceWaitError("ADVICE_CONTEXT_CHANGED"));
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
    if (!scope || !scopeIsActive(scope) || mutationLock.current || !buyer || !cap?.advice_configured || workflowBusy || !policyHash) return;
    const controller = new AbortController();mutationController.current=controller;
    const signal = AbortSignal.any([scope.controller.signal,controller.signal]);
    const startedFreshness = latestFreshnessEvent.current;
    const assertFresh = () => {
      signal.throwIfAborted();
      if (startedFreshness !== latestFreshnessEvent.current) throw new AdviceWaitError("ADVICE_CONTEXT_CHANGED");
    };
    mutationLock.current=true;setWorking(true);setRunError("");
    try {
      const run = pending || await waitForAdvice(waitSignal=>api<AdviceRun>(`/requests/${request.id}/advice-runs`,token,"POST",
        {expected_version:request.version,idempotency_key:crypto.randomUUID()},waitSignal),{signal,timeoutMs:ADVICE_WRITE_TIMEOUT_MS});
      if (!scopeIsActive(scope)) return; // Never start paid work for an abandoned reservation response.
      assertFresh();
      retain(scope,run);
      if (run.status === "PENDING" && bindingCurrent(run,policyHash) && run.request_version === request.version) {
        setProcessingRunId(run.id);processingNetworkPending.current=true;
        const result = await waitForAdvice(waitSignal=>api<AdviceRun>(`/advice-runs/${run.id}/process`,token,"POST",undefined,waitSignal),
          {signal,timeoutMs:ADVICE_WRITE_TIMEOUT_MS});
        assertFresh();
        retain(scope,result);
        if (scopeIsActive(scope)) setProcessingRunId(null);
        if (scopeIsActive(scope)) await reload(scope);
      }
    } catch (error) {
      processingNetworkPending.current=false;
      if (scopeIsActive(scope)) {
        setRunError(`${error instanceof Error ? error.message : String(error)}。未自动重试；正在回读状态。停止等待不代表服务端或提供方已取消，可能已产生费用。失败或中断后须显式新建运行。`);
        await reload(scope); // A lost response may still have a persisted terminal result.
      }
    } finally {
      processingNetworkPending.current=false;
      if (mutationController.current === controller) mutationController.current=null;
      mutationLock.current=false;
      if (scopeIsActive(scope)) setWorking(false);
    }
  };
  const freshnessVerified = !loading && !readError && verifiedFreshness === freshnessEvent;
  const isCurrent = (run:AdviceRun) => freshnessVerified && !!policyHash &&
    bindingCurrent(run,policyHash) && run.request_version === request.version;
  const activeRun = runs.some(run=>isCurrent(run) && ["PENDING","RUNNING"].includes(run.status));
  const citationDocument = (id:string) => quotes.find(quote=>id.startsWith(quote.document_id+":"));
  const openCitation = async(id:string) => {
    const scope=context.current,quote=citationDocument(id);
    if (!scope || !scopeIsActive(scope) || !quote || citationBusy) return;
    const ticket=beginEvidence();setCitationBusy(true);setCitationError("");
    try {
      const source = await waitForAdvice(signal=>api<DocumentEvidence>(`/documents/${quote.document_id}/evidence`,token,"GET",undefined,signal),
        {signal:scope.controller.signal});
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
      <p>绑定需求 v{request.version}、报价集合、生效策略与来源快照。建议不会审批、采购、生成 ERP 草稿或改变确定性规则结果。</p></div>
      <div className="heading-actions"><button className="secondary" data-testid="refresh-advice" disabled={loading||working}
        onClick={()=>{if (context.current) void reload(context.current);}}>回读建议状态</button>
        {working && <button className="secondary" data-testid="stop-advice-wait"
          onClick={()=>mutationController.current?.abort(new AdviceWaitError("ADVICE_WAIT_CANCELLED"))}>停止等待并回读状态</button>}
        {buyer && <button className="primary" data-testid="new-advice" disabled={workflowBusy||working||loading||!!readError||!policyHash||!cap?.advice_configured||activeRun}
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
          <span className={`badge ${isCurrent(run) ? "ok" : "warn"}`} data-testid="advice-current">{isCurrent(run) ? "当前版本及来源" : freshnessVerified ? "历史结果，已失效" : "当前性待核验"}</span></div>
        <p className="source-meta">运行 {run.id} · 创建于 {run.created_at}{run.completed_at ? ` · 结束于 ${run.completed_at}` : ""}</p>
        {!isCurrent(run) && <p data-testid="advice-stale">{freshnessVerified
          ? <>需求版本、报价集合、策略或证据来源已变化或无法核验，不能将旧建议视为当前结论。{staleExplanation(run.policy_hash && run.policy_hash!==policyHash?"POLICY_CHANGED":run.stale_reason)}（{run.stale_reason||"INPUT_BINDING_CHANGED"}）</>
          : "历史读取未完成或失败，当前性尚未核验，不能将缓存建议视为当前结论。"}</p>}
        {run.error_code && <p className="form-error" data-testid="advice-failure">{run.error_code}：{adviceFailureExplanation(run.error_code)}本次运行不会自动重试；核对来源、配置及状态后，如需再次调用，请显式新建运行。</p>}
        {run.status === "INTERRUPTED" && <p>调用结果不确定，可能已产生费用。此运行不能重放；如需再次调用，请新建运行。</p>}
        {run.status === "RUNNING" && <p>正在回读运行状态，页面刷新不会重放模型调用。</p>}
        {run.status === "PENDING" && <p>{processingRunId === run.id
          ? "回读结果前不能确认是否已经调用模型，可能产生费用。不会自动重试。"
          : "仅已预留，尚未执行。刷新或切换回来不会自动调用模型。"}</p>}
        {buyer && run.status === "PENDING" && isCurrent(run) && <button className="secondary" data-testid="process-advice"
          disabled={workflowBusy||working||loading||!!readError||!policyHash||!cap?.advice_configured} onClick={()=>void start(run)}>
          {processingRunId === run.id ? "重新提交此运行的处理请求" : "执行此已预留运行"}</button>}
        {run.output && <div data-testid="advice-output">
          <p style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere",marginTop:12}}>{run.output.summary}</p>
          <p>仅供参考 · 语义真实性未验证 · {run.output.evidence_read_verified ? "引用片段存在且已在运行中读取" : "引用只校验来源 ID 存在"}</p>
          <div data-testid="advice-citations" className="source-meta">引用来源：{run.output.evidence_ids.length ? run.output.evidence_ids.map(id=><div key={id}>
            {citationDocument(id) ? <button className="field-link" data-testid="advice-citation" disabled={citationBusy||workflowBusy}
              style={{textAlign:"left",overflowWrap:"anywhere"}} onClick={()=>void openCitation(id)}>核验并查看 {id}</button>
              : <span>{id}（不在当前报价来源中，无法打开）</span>}</div>) : "未引用来源片段"}</div>
          <p>模型对话 {run.output.model_calls} 轮 · {providerAttemptExplanation(run.output)} · 只读工具调用 {run.output.tool_calls} 次 · {run.output.usage_complete && run.output.usage
            ? `提供方报告 token 总量 ${run.output.usage.total_tokens}` : "token 用量不完整或未知"} · 费用未知</p>
          <details><summary>查看只读运行记录</summary><pre>{JSON.stringify({runtime:run.output.runtime,llm_used:run.output.llm_used,
            model_calls:run.output.model_calls,provider_attempts:run.output.provider_attempts??null,
            advisory_only:run.output.advisory_only,evidence_read_verified:run.output.evidence_read_verified,
            semantic_factuality_verified:run.output.semantic_factuality_verified,
            usage:run.output.usage,usage_complete:run.output.usage_complete,trace:run.output.trace},null,2)}</pre></details>
        </div>}
        <details><summary>查看输入快照与确切版本绑定</summary><dl className="binding-grid">
          <dt>策略版本</dt><dd>{run.policy_version?`v${run.policy_version}`:"旧记录未报告"}</dd><dt>策略哈希</dt><dd>{run.policy_hash||"旧记录未报告"}</dd>
          <dt>报价集合哈希</dt><dd>{run.quote_collection_hash||"旧记录未报告"}</dd><dt>输入哈希</dt><dd>{run.input_hash}</dd></dl>
          {run.input_snapshot&&<pre>{JSON.stringify(run.input_snapshot,null,2)}</pre>}</details>
      </article>)}</div>
      {!!runs.length && <p style={{marginTop:14}}>最多显示最近 20 次持久化运行。失败、中断或失效均不会触发自动重试。</p>}
    </div>
  </section>;
}

export default function Workbench() {
  return <SessionBoundary>{props=><AuthenticatedWorkbench key={props.token.id} {...props}/>}</SessionBoundary>;
}

function AuthenticatedWorkbench({token,me,cap,authControls}:AuthenticatedProps) {
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
  const [policy,setPolicy] = useState<PolicyVersion|null>(null);
  const [policyRefresh,setPolicyRefresh] = useState(0);
  const onPolicyRead = useCallback((current:PolicyVersion|null)=>setPolicy(current),[]);
  const uploadRef = useRef<HTMLInputElement>(null);
  const mutationLock = useRef(false);
  const readEpoch = useRef(0);
  const activeScope = useRef("");
  const evidenceReadEpoch = useRef(0);
  const buyer = me?.role === "buyer";
  const frozen = isFrozen(selected);
  const proposalCurrent = !!policy && !!selected?.proposal && selected.proposal_current !== false && selected.proposal.policy_hash === policy.policy_hash;
  const businessEvent = events.reduce((latest,event)=>["REQUEST_CHANGED","QUOTE_IMPORTED","QUOTE_CONFIRMED","QUOTE_VERSION_CREATED","POLICY_CHANGED","POLICY_CHECK_COMPLETED","PROPOSAL_PREPARED","ANALYSIS_BLOCKED","APPROVAL_APPROVED","APPROVAL_REJECTED"].includes(event.type) ? Math.max(latest,event.id) : latest,0);
  const freshnessEvent = `${selected?.version}:${selected?.status}:${businessEvent}:${policy?.policy_hash||"unknown"}`;

  const act = useCallback(async(fn:()=>Promise<void>,rethrow=false) => {
    if (mutationLock.current || !token.active) return;
    mutationLock.current = true; setBusy(true); setError("");
    try { await fn(); }
    catch (e) { if(token.active)setError(e instanceof Error ? e.message : String(e)); if(rethrow)throw e; }
    finally { mutationLock.current = false; if(token.active)setBusy(false); }
  },[token]);
  const clearContext = useCallback(() => {
    ++evidenceReadEpoch.current;
    setSelected(null); setQuotes([]); setEvents([]); setOperation(null); setEvidence(null); setVerification(null);
    setEditing(null); setConfirming(null); setRequestForm(null); setStream("idle");
  },[]);
  const refresh = useCallback(async(id:string, credential=token) => {
    token.assertCurrent();
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
    if (!token.active || epoch !== readEpoch.current) return; // A slower response must not overwrite a newer identity/request.
    setSelected(r); setQuotes(q); setEvents(e); setRequests(list); setOperation(op);
  },[token]);
  useEffect(()=>{
    let active=true;const epoch=++readEpoch.current;
    void api<ProcurementRequest[]>("/requests",token).then(async list=>{
      if(!active||!token.active||epoch!==readEpoch.current)return;
      setRequests(list);if(list.length)await refresh(list[0].id);
    }).catch(error=>{if(active&&token.active)setError(error instanceof Error?error.message:String(error));});
    return()=>{active=false;++readEpoch.current;++evidenceReadEpoch.current;};
  },[token,refresh]);
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
    }).catch(()=>{}); // Session invalidation already owns the global signed-out state.
    return ()=>controller.abort();
  },[selected?.id,token]);

  useEffect(()=>{
    if(!policy||!selected?.id||!token)return;
    const id=selected.id,credential=token,scope=activeScope.current,epoch=readEpoch.current;
    const controller=new AbortController();
    // Policy activation and source events refresh authoritative comparisons and stale proposal flags without writing anything.
    void Promise.all([
      api<ProcurementRequest>(`/requests/${id}`,credential,"GET",undefined,controller.signal),
      api<Quote[]>(`/requests/${id}/quotes`,credential,"GET",undefined,controller.signal),
    ]).then(([request,offers])=>{
      if(!controller.signal.aborted&&activeScope.current===scope&&readEpoch.current===epoch){setSelected(request);setQuotes(offers);}
    }).catch(e=>{if(!controller.signal.aborted&&activeScope.current===scope)setError(`策略绑定回读失败：${e instanceof Error?e.message:String(e)}`);});
    return()=>controller.abort();
  },[policy?.policy_hash,businessEvent,selected?.id,token]);

  useEffect(()=>{
    if(cap.mode!=="pilot"||!operation||!["PENDING","IN_FLIGHT","RECONCILING"].includes(operation.status))return;
    const controller=new AbortController(),id=operation.id,scope=activeScope.current;
    const timer=setTimeout(()=>{
      void api<Operation>(`/operations/${id}`,token,"GET",undefined,controller.signal).then(saved=>{
        if(!controller.signal.aborted&&token.active&&activeScope.current===scope){
          setOperation(saved);if(saved.status!==operation.status&&selected)void refresh(selected.id).catch(()=>{});
        }
      }).catch(error=>{if(!controller.signal.aborted&&token.active)setError(`执行状态回读失败：${error instanceof Error?error.message:String(error)}`);});
    },2000);
    return()=>{clearTimeout(timer);controller.abort();};
  },[cap.mode,operation,token,refresh,selected?.id]);

  const saveRequest = async(body:Record<string,unknown>) => {
    const target=requestForm;
    await act(async()=>{
      const r = target && target !== "new"
        ? await api<ProcurementRequest>(`/requests/${target.id}`,token,"PUT",{...body,expected_version:target.version})
        : await api<ProcurementRequest>("/requests",token,"POST",body);
      setRequestForm(null); await refresh(r.id);
    },true);
  };
  const saveQuote = async(values:Record<string,unknown>,reason:string) => {
    if (!editing) return;
    const q = editing;
    await act(async()=>{
      await api(`/quotes/${q.id}`,token,"PUT",{expected_version:q.version,values,reason});
      setEditing(null); await refresh(q.request_id);
    },true);
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
    if (!selected?.proposal || !proposalCurrent) return;
    await api(`/requests/${selected.id}/approval`,token,"POST",{snapshot_hash:selected.proposal.snapshot_hash,decision});
    await refresh(selected.id);
  });
  const execute = () => act(async()=>{
    if (!selected?.proposal || !proposalCurrent) return;
    const op = await api<Operation>(`/requests/${selected.id}/execute`,token,"POST",{snapshot_hash:selected.proposal.snapshot_hash});
    setOperation(op);
    try { if(cap.mode!=="pilot")await api(`/operations/${op.id}/process`,token,"POST"); }
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
      <header className="topbar" style={{height:"auto",minHeight:74,paddingTop:12,paddingBottom:12}}><span>工作区 / 采购中心</span>
        {authControls}
      </header>
      <section className="content" aria-busy={busy}>
        <div className="page-heading"><div><div className="eyebrow">PROCUREMENT / WORKBENCH</div>
          <h1>让每一笔采购，都有据可依。</h1><p>字段证据、确定性金额、版本绑定审批与受控执行。</p></div>
          <div className="heading-actions">{cap?.demo_samples && <button data-testid="load-demo" className="secondary" disabled={!buyer||busy} onClick={()=>void demo()}>载入三份示例</button>}
            <button className="primary" disabled={!buyer||busy} onClick={()=>setRequestForm("new")}>＋ 新建采购需求</button></div>
        </div>
        <div className="notice">本地 Alpha：金额由确定性规则计算，不是 LLM 推理。当前 ERP：{cap?.erp_mode||"尚未认证"}。正式提交始终不在本工作台权限内。</div>
        {error && <div role="alert" className="form-error">{error}</div>}
        {me && <PolicyPanel key={token.id} token={token} approver={me.role==="approver"} workflowBusy={busy}
          refreshEvent={policyRefresh+events.filter(event=>event.type==="POLICY_CHANGED").length} onRead={onPolicyRead}/>}
        <RecoveryPanel key={`recovery-${token.id}`} token={token} workflowBusy={busy} onOpenRequest={id=>void act(()=>choose(id))}/>
        <div className="metrics">{[["采购需求",requests.length],["报价版本",quotes.length],["已确认",quotes.filter(q=>q.confirmed_by).length],["审计记录",events.length]].map(([label,count])=>
          <div className="metric" key={label}><label>{label}</label><strong>{count}</strong></div>)}</div>
        {!selected ? <section className="empty panel"><h2>{me ? "创建或选择一项采购需求" : "登录后开始采购核对"}</h2>
          <p>{cap.mode==="demo"?"演示环境采用合成数据与独立身份，不会写入真实 ERP。":"受控工作区中的操作受当前租户与角色权限约束。"}已配置模型仅在显式生成只读建议时调用。</p>
        </section> : <>
          <section className="panel request-panel"><div><div className="eyebrow">{selected.id}</div><h2>{selected.title}</h2>
            <p data-testid="request-meta">{selected.lines?.length?`${selected.lines.length} 项物料`: `${selected.sku} · ${selected.quantity} ${selected.uom}`} · 预算 ¥{selected.budget} · ≤ {selected.max_delivery_days} 天 · v{selected.version}</p>
            {!!selected.lines?.length&&<ul data-testid="request-lines" className="request-lines">{selected.lines.map((line,index)=><li key={line.sku}>{index+1}. {line.sku} · {line.quantity} {line.uom}</li>)}</ul>}
            {policy&&<p data-testid="effective-limits">当前策略 v{policy.version} · 实际预算 ≤ ¥{strictestBudget(selected.budget,policy.budget_cap)} · 实际交期 ≤ {Math.min(selected.max_delivery_days,policy.max_delivery_days??selected.max_delivery_days)} 天 · 至少 {policy.minimum_valid_quotes} 家合规供应商</p>}</div>
            <div className="heading-actions"><span className="badge" data-testid="request-status">{!frozen&&selected.proposal&&!proposalCurrent&&selected.status==="APPROVED"?"审批已失效":names[selected.status]||selected.status}</span>
              <button className="secondary" disabled={busy} onClick={()=>void act(async()=>{setPolicyRefresh(value=>value+1);await refresh(selected.id);})}>刷新状态</button>
              <button className="secondary" data-testid="edit-request" disabled={!buyer||frozen||busy} onClick={()=>setRequestForm(selected)}>修改需求</button>
            </div>
          </section>
          <section className="panel quote-panel"><div className="section-head"><div><h2>供应商报价比较</h2><p>未知不是零。点击数值查看字段来源；确认不会补全缺失值。</p></div>
            <div className="heading-actions"><input ref={uploadRef} type="file" hidden accept=".txt,.csv,.pdf,.xlsx" onChange={e=>{
              const file=e.target.files?.[0]; e.target.value="";
              if (file) void act(async()=>{const form=new FormData();form.append("file",file);await api(`/requests/${selected.id}/documents`,token,"POST",form);await refresh(selected.id);});
            }}/><button className="secondary" disabled={!buyer||frozen||busy} onClick={()=>uploadRef.current?.click()}>上传报价</button>
              <TableImportButton key={token+"\n"+selected.id} token={token} requestId={selected.id} requestVersion={selected.version} multiItem={!!selected.lines?.length} disabled={!buyer||frozen} workflowBusy={busy} onImported={()=>{
                const scope=token+"\n"+selected.id;if(activeScope.current===scope)void act(()=>refresh(selected.id));
              }}/>
              <button className="primary" data-testid="analyze" disabled={!buyer||frozen||busy||!policy} onClick={()=>void act(async()=>{await api(`/requests/${selected.id}/analyze`,token,"POST",{});await refresh(selected.id);})}>校验并生成方案</button>
            </div></div>
            <div className="table-scroll"><table><thead><tr><th>供应商</th><th>单价</th><th>税价</th><th>运费</th><th>统一总价</th><th>核对状态</th><th>操作</th></tr></thead>
              <tbody data-testid="quote-body">{quotes.map(q=><Fragment key={q.id}><tr data-testid="quote-row"><td><strong>{q.values.supplier_id||"未知供应商"}</strong><small>{q.filename} · v{q.version}</small></td>
                {(["unit_price","tax_mode","shipping_cost"] as const).map(key=><td key={key}>{q.values.lines?.length&&key!=="shipping_cost"?<span>{key==="unit_price"?`${q.values.lines.length} 项明细`:"逐项税价"}</span>:<button className="field-link" data-testid={`field-${key}`} onClick={()=>{++evidenceReadEpoch.current;setEvidence(q.evidence[key]||{kind:"unknown"});}}>{q.values[key]??"未知"}</button>}</td>)}
                <td><strong>{q.calculation.total??"未知"}</strong></td><td><span className="badge">{q.confirmed_by?"已确认":"待核对"}</span>
                  <details><summary className="table-action">{q.calculation.violations.length?`${q.calculation.violations.length} 项未通过`:"查看版本与限制"}</summary>
                    <div className="source-meta">报价 {q.id} · 版本 ID {q.version_id}</div>
                    {q.calculation.effective_limits&&<p>预算 ≤ ¥{q.calculation.effective_limits.budget} · 交期 ≤ {q.calculation.effective_limits.max_delivery_days} 天</p>}
                    <ul className="violation-list">{q.calculation.violations.map(code=><li key={code}>{violationExplanation(code)}（{code}）</li>)}</ul></details></td><td><div className="row-actions">
                    <button className="secondary" disabled={!buyer||frozen||busy} onClick={()=>setEditing(q)}>修正字段</button>
                    <button className="secondary" data-testid="confirm-quote" disabled={!buyer||frozen||busy||!!q.confirmed_by} onClick={()=>setConfirming(q)}>确认字段</button>
                  </div></td></tr>{(!!q.values.lines?.length||!!selected.lines?.length)&&<tr className="quote-line-row"><td colSpan={7}><QuoteLineDetails forceLines={!!selected.lines?.length} values={q.values} calculation={q.calculation} evidence={q.evidence} onEvidence={source=>{++evidenceReadEpoch.current;setEvidence(source);}}/></td></tr>}</Fragment>)}</tbody></table></div>
          </section>
          <div className="lower-grid"><section className="panel"><div className="section-head"><h2>字段证据</h2></div><div id="evidence-body" className="evidence-body" data-testid="evidence-body">
            {evidence ? <><pre>{evidence.text||evidence.reason||"该字段没有原文支持，保持未知。"}</pre>
              <div className="source-meta">{evidence.page?`第 ${evidence.page} 页 / 行 ${evidence.line}`:evidence.cell_range?`${evidence.sheet}!${evidence.cell_range}`:evidence.line?`文本第 ${evidence.line} 行`:evidence.kind}<br/>{evidence.document_sha256}</div>
              {evidence.sources?.map((source,index)=><div key={index} className="source-meta">{source.text||source.reason||"未知"} · {source.sheet}{source.cell_range?`!${source.cell_range}`:source.row?` 第 ${source.row} 行`:""}</div>)}</>
              : <p className="subtle-empty">选择一项报价字段</p>}
          </div></section>
          <section className="panel"><div className="section-head"><h2>方案与审批</h2></div><div className="decision-content" data-testid="proposal-body">
            {selected.proposal ? <><div className="decision-label">{selected.proposal.quote_values.supplier_id}</div><div className="decision-amount">¥ {selected.proposal.total}</div>
              <p>报价版本 v{selected.proposal.quote_version} · {selected.proposal.quote_values.lines?.length?`${selected.proposal.quote_values.lines.length} 项物料，完整整单`: `数量 ${selected.proposal.quote_values.quantity} · ${selected.proposal.quote_values.uom}`}</p>
              <QuoteLineDetails forceLines={!!selected.lines?.length} values={selected.proposal.quote_values} calculation={selected.proposal.quote_collection.find(quote=>quote.id===selected.proposal!.quote_id)?.calculation} testId="proposal-line-details"/>
              {!!selected.proposal.quote_values.lines?.length&&<p className="stale-notice" data-testid="multi-erp-boundary">多物料比较和模拟 ERP 支持逐项税额与折扣。真实 ERPNext 草稿当前仅支持整数量 EA、CNY 总额不超过 100 万、零税率、零商品折扣、各项税价模式一致且明确的报价；超出边界会被服务端拒绝。</p>}
              <div className="hash">{selected.proposal.snapshot_hash}</div>
              <p data-testid="proposal-policy-binding">策略 v{selected.proposal.policy_version} · 报价 {selected.proposal.quote_id} v{selected.proposal.quote_version}</p>
              <details><summary>查看方案完整绑定</summary><dl className="binding-grid"><dt>策略哈希</dt><dd>{selected.proposal.policy_hash}</dd><dt>报价集合哈希</dt><dd>{selected.proposal.quote_collection_hash}</dd></dl><pre>{JSON.stringify({policy:selected.proposal.policy,quote_collection:selected.proposal.quote_collection},null,2)}</pre></details>
              {!proposalCurrent&&!frozen&&<p className="stale-notice" data-testid="proposal-stale">方案和审批绑定已失效或无法核验。{staleExplanation(selected.proposal.policy_hash!==policy?.policy_hash?"POLICY_CHANGED":selected.proposal_stale_reason)}；不能审批或创建新 ERP 草稿。</p>}
              {frozen&&!proposalCurrent&&<p data-testid="reserved-policy-binding">当前策略已变化或尚未核验。尚未写入的操作会再次检查策略；已完成或结果不确定的操作仅按原快照只读核对，不重放。</p>}
              {me?.role === "approver" && !frozen && <div className="heading-actions">
                <button className="primary" data-testid="approve" disabled={busy||!proposalCurrent} onClick={()=>void approve("approve")}>批准展示的快照</button>
                <button className="secondary" data-testid="reject" disabled={busy||!proposalCurrent} onClick={()=>void approve("reject")}>拒绝此方案</button></div>}
              {buyer && selected.status === "APPROVED" && proposalCurrent && <button className="primary" data-testid="execute" disabled={busy||!cap?.erp_draft_writes_enabled} onClick={()=>void execute()}>
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
                {cap.mode==="pilot"&&operation.status!=="COMPLETED"&&<p>执行已预留，交由独立 Worker 处理。退出登录不会撤回已接受的执行；撤销用户或角色权限会阻止尚未发送的首次写入。请回读最终结果。</p>}
                {buyer && cap.mode!=="pilot" && operation.status !== "COMPLETED" && <button className="secondary" disabled={busy} onClick={()=>void act(async()=>{
                  await api(`/operations/${operation.id}/process`,token,"POST"); await refresh(selected.id);
                })}>处理 / 只读核对</button>}</>}
            </> : <p className="subtle-empty">核对报价后生成方案；需求、报价或策略变化后需要重新生成和审批。</p>}
          </div></section></div>
          <EvaluationPanel key={token+"\n"+selected.id} token={token} requestId={selected.id} policyHash={policy?.policy_hash||null} refreshEvent={freshnessEvent}/>
          <AdvicePanel key={token+"\n"+selected.id+"\n"+selected.version} request={selected} token={token} cap={cap} buyer={buyer} workflowBusy={busy}
            quotes={quotes} freshnessEvent={freshnessEvent} policyHash={policy?.policy_hash||null}
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
    {requestForm && <RequestEditor key={requestForm==="new"?"new":`${requestForm.id}:${requestForm.version}`} request={requestForm} busy={busy} onClose={()=>setRequestForm(null)} onSave={saveRequest}/>}
    {editing && selected && <QuoteEditor key={`${editing.id}:${editing.version}`} quote={editing} request={selected} busy={busy} onClose={()=>setEditing(null)} onSave={saveQuote} onEvidence={source=>{++evidenceReadEpoch.current;setEvidence(source);}}/>}
    {confirming && <dialog open aria-labelledby="confirm-title"><div className="modal-head"><h2 id="confirm-title">核对 {confirming.values.supplier_id} · v{confirming.version}</h2>
      <button aria-label="关闭确认表单" disabled={busy} onClick={()=>setConfirming(null)}>×</button></div>
      <form onSubmit={confirm}><p>请逐项检查原始报价。确认只记录核对人，不会把未知运费、税率等变成零。</p>
        <div className="confirm-values">{(confirming.values.lines?.length?HEADER_FIELDS:fields).map(key=><div key={key}><strong>{labels[key]}：</strong>{confirming.values[key]??"未知"}</div>)}</div>
        <QuoteLineDetails forceLines={!!selected?.lines?.length} values={confirming.values} calculation={confirming.calculation}/>
        {!!confirming.calculation.violations.length&&<ul className="violation-list" data-testid="confirm-violations">{confirming.calculation.violations.map(code=><li key={code}>{violationExplanation(code)}（{code}）</li>)}</ul>}
        <label className="ack-label"><input type="checkbox" name="acknowledge" required data-testid="ack-confirm"/>我已核对展示的报价版本，保留所有未知字段。</label>
        <div className="form-actions"><button type="submit" className="primary" data-testid="submit-confirm" disabled={busy}>确认此版本</button></div>
      </form></dialog>}
  </>;
}
