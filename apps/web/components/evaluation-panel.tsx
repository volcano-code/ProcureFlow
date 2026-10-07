"use client";
import type {Credential} from "@/lib/session.mjs";
import {useCallback,useEffect,useRef,useState} from "react";
import {api,type Evaluation,type ApprovalReceipt} from "@/lib/api";
import {bindingCurrent,staleExplanation,violationExplanation} from "@/lib/policy.mjs";

export default function EvaluationPanel({token,requestId,policyHash,refreshEvent}:{token:Credential;requestId:string;policyHash:string|null;refreshEvent:string}) {
  const [evaluations,setEvaluations]=useState<Evaluation[]>([]);
  const [approvals,setApprovals]=useState<ApprovalReceipt[]>([]);
  const [loading,setLoading]=useState(true),[error,setError]=useState("");
  const scope=useRef<{active:boolean;controller:AbortController}|null>(null),epoch=useRef(0);
  const reload=useCallback(async()=>{
    const active=scope.current;if(!active?.active)return;
    const ticket=++epoch.current;setLoading(true);setError("");
    try {
      const [data,receipts]=await Promise.all([
        api<Evaluation[]>(`/requests/${requestId}/evaluations`,token,"GET",undefined,active.controller.signal),
        api<ApprovalReceipt[]>(`/requests/${requestId}/approvals`,token,"GET",undefined,active.controller.signal),
      ]);
      if(active.active&&scope.current===active&&ticket===epoch.current){setEvaluations(data);setApprovals(receipts);}
    }catch(e){if(active.active&&scope.current===active&&ticket===epoch.current)setError(e instanceof Error?e.message:String(e));}
    finally{if(active.active&&scope.current===active&&ticket===epoch.current)setLoading(false);}
  },[token,requestId]);
  useEffect(()=>{
    const active={active:true,controller:new AbortController()};scope.current=active;void reload();
    return()=>{active.active=false;active.controller.abort();};
  },[reload,refreshEvent]);
  return <section className="panel quote-panel evaluation-panel" data-testid="evaluation-panel" aria-labelledby="evaluation-title" aria-busy={loading}>
    <div className="section-head"><div><h2 id="evaluation-title">确定性评估与审批历史</h2><p>每次校验保留当时的需求、全部报价和策略绑定。旧记录不可改写，也不能替代当前审批。</p></div>
      <button className="secondary" data-testid="refresh-evaluations" disabled={loading} onClick={()=>void reload()}>回读评估历史</button></div>
    <div className="decision-content">
      {error&&<p role="alert" className="form-error">评估历史读取失败：{error}</p>}
      {loading&&<p role="status">正在回读评估…</p>}
      {!loading&&!error&&!evaluations.length&&<p className="subtle-empty">尚无已保存的确定性评估。</p>}
      {evaluations.map((evaluation,index)=>{
        const current=!!policyHash&&bindingCurrent(evaluation,policyHash);
        const reason=evaluation.policy_hash!==policyHash?"POLICY_CHANGED":evaluation.stale_reason;
        return <article key={evaluation.id} className="evaluation-version" data-testid="evaluation">
          <div className="heading-actions"><strong>{index===0?"最近一次评估":"历史评估"}</strong><span className={`badge ${current?"ok":"warn"}`} data-testid="evaluation-current">{current?"绑定仍有效":"已失效，仅供历史查阅"}</span>
            <span className="badge">策略 v{evaluation.policy_version} · 需求 v{evaluation.request_version}</span></div>
          <p>{evaluation.created_at} · {evaluation.created_by} · 合规供应商 {evaluation.result.valid_quote_count} / 至少 {evaluation.result.minimum_valid_quotes} 家</p>
          {!current&&<p className="stale-notice" data-testid="evaluation-stale">{staleExplanation(reason)}（{reason||"POLICY_NOT_VERIFIED"}）</p>}
          <ul className="violation-list" data-testid="evaluation-violations">{evaluation.result.violations.map(code=><li key={code}>{violationExplanation(code)}（{code}）</li>)}</ul>
          <p>{evaluation.result.proposal?`当时方案：${evaluation.result.proposal.quote_values.supplier_id} · ¥ ${evaluation.result.proposal.total}`:"当时未生成可审批方案"}</p>
          <details><summary>查看确切版本绑定与逐项违规原因</summary>
            <dl className="binding-grid"><dt>评估 ID</dt><dd>{evaluation.id}</dd><dt>策略哈希</dt><dd>{evaluation.policy_hash}</dd><dt>报价集合哈希</dt><dd>{evaluation.quote_collection_hash}</dd><dt>输入快照哈希</dt><dd>{evaluation.input_hash}</dd></dl>
            {evaluation.result.quotes.map(quote=><div className="policy-version" key={quote.id}><strong>{quote.values.supplier_id||"未知供应商"} · 报价 v{quote.version}</strong>
              <p>{quote.id} · {quote.version_id}<br/>总价 {quote.calculation.total??"未知"} · {quote.calculation.eligible?"当时合规":"当时未通过"}</p>
              {quote.calculation.effective_limits&&<p>当时限制：预算 ¥{quote.calculation.effective_limits.budget} · 交期 ≤ {quote.calculation.effective_limits.max_delivery_days} 天</p>}
              <ul className="violation-list">{quote.calculation.violations.map(code=><li key={code}>{violationExplanation(code)}（{code}）</li>)}</ul>
            </div>)}
            <details><summary>查看原始不可变输入快照</summary><pre>{JSON.stringify(evaluation.input_snapshot,null,2)}</pre></details>
          </details>
        </article>;
      })}
      <details data-testid="approval-history"><summary>查看审批记录与不可变快照（{approvals.length} 条）</summary>
        {approvals.map(approval=>{
          const current=!!policyHash&&approval.current&&!!approval.snapshot&&approval.snapshot.policy_hash===policyHash;
          return <article key={approval.id} className="evaluation-version" data-testid="approval-receipt">
            <div className="heading-actions"><strong>{approval.stored_status==="APPROVED"?"记录状态：批准":approval.stored_status==="REJECTED"?"记录状态：拒绝":`记录状态：${approval.stored_status==="STALE"?"已失效":approval.stored_status==="SUPERSEDED"?"被后续审批替代":approval.stored_status}`}</strong>
              <span className={`badge ${current?"ok":"warn"}`} data-testid="approval-current">{current?"绑定仍有效":"已失效，仅供审计"}</span></div>
            <p>{approval.approver_id} · {approval.created_at}<br/>审批有效期截至 {approval.expires_at}</p>
            {!current&&<p className="stale-notice">{staleExplanation(approval.stale_reason)}（{approval.stale_reason||"POLICY_CHANGED"}）</p>}
            {approval.note&&<p>审批备注：{approval.note}</p>}
            <dl className="binding-grid"><dt>审批 ID</dt><dd>{approval.id}</dd><dt>原快照哈希</dt><dd>{approval.snapshot_hash}</dd>
              <dt>绑定策略</dt><dd>{approval.snapshot?`v${approval.snapshot.policy_version} · ${approval.snapshot.policy_hash}`:"旧记录未保存可核验快照"}</dd>
              <dt>报价集合哈希</dt><dd>{approval.snapshot?.quote_collection_hash||"旧记录未保存"}</dd></dl>
            {approval.snapshot&&<details><summary>查看批准时的完整快照</summary><pre>{JSON.stringify(approval.snapshot,null,2)}</pre></details>}
          </article>;
        })}
        {!approvals.length&&<p>尚无已保存审批记录。</p>}
      </details>
    </div>
  </section>;
}
