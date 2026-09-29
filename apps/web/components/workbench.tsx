"use client";
import {useCallback, useEffect, useRef, useState, type FormEvent} from "react";
import {API, api, loadDemo, watchAudit, type Identity, type Capabilities, type ProcurementRequest,
  type Quote, type QuoteValues, type EvidenceRef, type AuditEvent, type Operation} from "@/lib/api";

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
    setSelected(null); setQuotes([]); setEvents([]); setOperation(null); setEvidence(null); setVerification(null);
    setEditing(null); setConfirming(null); setRequestForm(null); setStream("idle");
  },[]);
  const refresh = useCallback(async(id:string, credential=token) => {
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
          <p>演示环境采用合成数据与独立身份，不会调用模型或真实 ERP。</p>
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
                {(["unit_price","tax_mode","shipping_cost"] as const).map(key=><td key={key}><button className="field-link" data-testid={`field-${key}`} onClick={()=>setEvidence(q.evidence[key]||{kind:"unknown"})}>{q.values[key]??"未知"}</button></td>)}
                <td><strong>{q.calculation.total??"未知"}</strong></td><td><span className="badge">{q.confirmed_by?"已确认":"待核对"}</span>
                  <small>{q.calculation.violations.join(" / ")}</small></td><td><div className="row-actions">
                    <button className="secondary" disabled={!buyer||frozen||busy} onClick={()=>setEditing(q)}>修正字段</button>
                    <button className="secondary" data-testid="confirm-quote" disabled={!buyer||frozen||busy||!!q.confirmed_by} onClick={()=>setConfirming(q)}>确认字段</button>
                  </div></td></tr>)}</tbody></table></div>
          </section>
          <div className="lower-grid"><section className="panel"><div className="section-head"><h2>字段证据</h2></div><div className="evidence-body" data-testid="evidence-body">
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
          <section className="panel"><div className="section-head"><h2>审计时间线</h2><span className="live" data-testid="stream-status">SSE · {stream}</span></div>
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
