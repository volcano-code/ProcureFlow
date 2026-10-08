"use client";
import {useCallback,useEffect,useRef,useState} from "react";
import {API,type RecoveryPage,type RecoveryDetail,type RecoveryReconciliation} from "@/lib/api";
import type {SessionScope} from "@/lib/session.mjs";
import {RecoveryReader,assertReconciliationBinding,reconciliationMessage,type RecoveryReadTask} from "@/lib/recovery.mjs";

type Props={token:SessionScope;workflowBusy:boolean;onOpenRequest:(id:string)=>void};
type Context={reader:RecoveryReader;active:boolean};
const failure=(error:unknown)=>error instanceof Error?error.message:String(error);
const value=(item:unknown)=>item===undefined?"未返回":item===null?"未报告":typeof item==="boolean"?(item?"是":"否"):
  typeof item==="object"?JSON.stringify(item):String(item);
const when=(date:string)=>Number.isFinite(Date.parse(date))?new Date(date).toLocaleString():date||"未报告";
const statusLabels:Record<string,string>={PENDING:"等待执行",IN_FLIGHT:"执行结果待确认",RECONCILING:"结果待核对",
  COMPLETED:"历史完成记录",SUCCEEDED:"历史成功记录",FAILED:"历史失败记录",NEEDS_HUMAN:"需要人工处理",RECOVERY_HELD:"恢复隔离",HELD:"已隔离"};
const stateLabels:Record<string,string>={ACTIVE:"服务已启用",PAUSED:"写入已暂停",RECOVERY:"恢复后待操作员检查"};
const integrityLabels={verified:"原始文件完整性已核验",missing:"原始文件缺失",mismatch:"原始文件哈希不符",unavailable:"无法核验原始文件"};
const fieldLabels:Record<string,string>={operation_key:"操作幂等键",snapshot_hash:"审批快照哈希",supplier_id:"供应商编码",sku:"型号",
  quantity:"数量",unit_price:"单价",total:"总额",currency:"币种",uom:"单位",company:"ERP 公司",transaction_date:"单据日期",docstatus:"单据状态（0 为草稿）",
  simulated:"模拟模式",name:"远端单据 ID",cost_contract:"费用映射约定"};
const diagnostics:Record<string,string>={RECOVERY_HOLD_PERMANENT:"恢复隔离保持有效",OBSERVATION_NEVER_AUTHORIZES_REPLAY:"任何观测结果都不授予重放权限",
  APPROVER_AUTHORITY_NOT_CURRENT:"原审批人的权限已失效",APPROVER_AUTHORITY_NOT_EVALUATED_OFFLINE:"离线诊断未核验原审批人当前权限",
  APPROVAL_EXPIRED:"原审批已到期",APPROVAL_NOT_CURRENT:"原审批状态或快照已失效",APPROVAL_MISSING:"原审批记录缺失",
  LOCAL_SNAPSHOT_INTEGRITY_FAILED:"本地执行快照完整性核验失败",SOURCE_BINDING_INVALID:"来源版本绑定无效",
  SOURCE_MISSING:"原始来源文件缺失",SOURCE_MISMATCH:"原始来源文件哈希不符",SOURCE_UNAVAILABLE:"无法核验来源文件或版本绑定",
  EXECUTION_TARGET_OR_SNAPSHOT_CHANGED:"执行目标或原快照已变化",REMOTE_ABSENCE_NOT_PROOF_OF_NO_COMMIT:"查无远端记录不能证明未提交",
  ERP_MALFORMED_RESPONSE:"ERP 返回格式无效",REMOTE_PAYLOAD_MISMATCH:"远端字段与原快照不一致",REMOTE_CONTRACT_MISMATCH:"远端费用约定不一致",
  ERP_DOCUMENT_REJECTED:"ERP 单据核验被拒绝",ERP_VERIFICATION_UNAVAILABLE:"ERP 核验暂不可用",LOCAL_LEDGER_CHANGED_DURING_READ:"核对期间本地账本已变化",
  VALUE_MISMATCH:"字段值不一致",INVALID_REMOTE_ID:"远端单据 ID 无效",INVALID_COST_PROOF:"费用证明格式无效",
  COST_FIELDS_MISMATCH:"费用字段集合不一致",COST_ROWS_MISMATCH:"费用行数不一致"};
const explain=(code:string)=>diagnostics[code]?`${diagnostics[code]}（${code}）`:code;
const yesNo=(flag:boolean)=>flag?"是":"否";

/** Mounted only inside a live authenticated workbench. Holds are never released here. */
export default function RecoveryPanel({token,workflowBusy,onOpenRequest}:Props) {
  const [page,setPage]=useState<RecoveryPage|null>(null),[cursor,setCursor]=useState<string|null>(null);
  const [loading,setLoading]=useState(true),[listError,setListError]=useState("");
  const [selectedId,setSelectedId]=useState<string|null>(null),[detail,setDetail]=useState<RecoveryDetail|null>(null);
  const [detailLoading,setDetailLoading]=useState(false),[detailError,setDetailError]=useState("");
  const [result,setResult]=useState<RecoveryReconciliation|null>(null),[reconciling,setReconciling]=useState(false);
  const [reconcileError,setReconcileError]=useState(""),[notice,setNotice]=useState("");
  const context=useRef<Context|null>(null);
  const current=useCallback((scope:Context,task:RecoveryReadTask<unknown>)=>
    context.current===scope&&scope.active&&token.active&&!task.signal.aborted,[token]);
  const clearDetail=useCallback(()=>{
    context.current?.reader.cancel("detail");context.current?.reader.cancel("reconciliation");
    setSelectedId(null);setDetail(null);setDetailLoading(false);setDetailError("");
    setResult(null);setReconciling(false);setReconcileError("");setNotice("");
  },[]);
  const readPage=useCallback(async(after:string|null)=>{
    const scope=context.current;if(!scope?.active||!token.active)return;
    const task=scope.reader.read("list",after);
    clearDetail();setCursor(after);setPage(null);setLoading(true);setListError("");
    try {
      const saved=await task.promise;
      if(current(scope,task))setPage(saved);
    } catch(error) {if(current(scope,task))setListError(`恢复列表读取失败：${failure(error)}。没有触发重放或外部写入。`);}
    finally {if(current(scope,task))setLoading(false);}
  },[token,current,clearDetail]);
  useEffect(()=>{
    const scope={reader:new RecoveryReader(API,token),active:true};context.current=scope;void readPage(null);
    return()=>{scope.active=false;scope.reader.close();};
  },[token,readPage]);
  const openDetail=async(id:string)=>{
    const scope=context.current;if(!scope?.active||!token.active)return;
    scope.reader.cancel("reconciliation");setResult(null);setReconciling(false);setReconcileError("");setNotice("");
    const task=scope.reader.read("detail",id);
    setSelectedId(id);setDetail(null);setDetailLoading(true);setDetailError("");
    try {
      const saved=await task.promise;
      if(saved.id!==id||saved.replay_permitted!==false)throw new Error("详情身份或只读约束不一致，请联系操作员核验");
      if(current(scope,task))setDetail(saved);
    } catch(error) {if(current(scope,task))setDetailError(`操作详情读取失败：${failure(error)}`);}
    finally {if(current(scope,task))setDetailLoading(false);}
  };
  const reconcile=async()=>{
    const scope=context.current;if(!scope?.active||!token.active||!detail)return;
    const task=scope.reader.read("reconciliation",detail.id);
    setReconciling(true);setResult(null);setReconcileError("");setNotice("");
    try {
      const saved=assertReconciliationBinding(await task.promise,detail);
      if(current(scope,task))setResult(saved);
    } catch(error) {if(current(scope,task))setReconcileError(`只读核对失败：${failure(error)}。远端结果仍不确定，禁止重放。不会自动重试。`);}
    finally {if(current(scope,task))setReconciling(false);}
  };
  return <section className="panel quote-panel recovery-panel" data-testid="recovery-panel" aria-labelledby="recovery-title">
    <div className="section-head"><div><h2 id="recovery-title">恢复诊断（只读）</h2>
      <p>查看恢复后被隔离的执行记录。核对不会解除隔离、刷新审批权限或重放 ERP 写入。</p></div>
      <button className="secondary" data-testid="refresh-recovery" disabled={loading} onClick={()=>void readPage(null)}>刷新隔离列表</button></div>
    <div className="decision-content">
      <p className="stale-notice" data-testid="recovery-safety">服务恢复与历史操作放行是不同事项。即使服务已恢复或远端草稿一致，历史操作仍保持隔离；来源完整性和审批状态仅供诊断。</p>
      {loading&&<p role="status">正在读取隔离操作…</p>}
      {listError&&<div role="alert" className="form-error" data-testid="recovery-list-error">{listError}
        <button className="secondary" onClick={()=>void readPage(cursor)}>重试读取此页</button></div>}
      {page&&<>
        <dl className="binding-grid" data-testid="recovery-state"><dt>当前服务状态</dt><dd>{stateLabels[page.state.state]||page.state.state}（{page.state.state}）</dd>
          <dt>恢复代次</dt><dd>{page.state.generation}</dd><dt>当前恢复 ID</dt><dd>{page.state.restore_id||"无"}</dd>
          <dt>要求的认证模式</dt><dd>{page.state.required_auth_mode||"未报告"}</dd></dl>
        <p>当前租户共有 {page.total} 项隔离记录；本页 {page.items.length} 项。不会自动核对远端。</p>
        {!page.items.length&&<p data-testid="recovery-empty">暂无隔离操作。此列表不代表其他操作的 ERP 提交结果。</p>}
        {!!page.items.length&&<div className="table-scroll"><table><caption className="sr-only">恢复隔离的执行记录</caption>
          <thead><tr><th>采购需求 / 操作</th><th>本地历史状态</th><th>远端记录</th><th>恢复隔离</th><th>只读详情</th></tr></thead>
          <tbody>{page.items.map(item=><tr key={item.id} data-testid="recovery-operation" aria-selected={selectedId===item.id}>
            <td><strong>{item.request_title||item.request_id}</strong><span className="source-meta">{item.id}</span><p>{when(item.created_at)}</p></td>
            <td>{statusLabels[item.status]||item.status}<p>尝试次数：{item.attempts}</p><p>Outbox：{item.outbox_status||"无记录"}</p></td>
            <td>{item.remote_id||"未记录远端 ID"}{item.error&&<p className="form-error">{item.error}</p>}</td>
            <td><span className="badge warn">保持隔离</span><p className="source-meta">{item.hold.restore_id}</p><p>原状态：{statusLabels[item.hold.original_status]||item.hold.original_status}</p></td>
            <td><button className="secondary" data-testid="open-recovery-detail" aria-expanded={selectedId===item.id}
              aria-controls="recovery-detail" onClick={()=>void openDetail(item.id)}>查看诊断</button></td>
          </tr>)}</tbody></table></div>}
        <div className="heading-actions recovery-pagination">
          {cursor&&<button className="secondary" data-testid="recovery-first-page" onClick={()=>void readPage(null)}>返回第一页</button>}
          {page.next_after&&<button className="secondary" data-testid="recovery-next-page" onClick={()=>void readPage(page.next_after)}>下一页</button>}
        </div>
      </>}
      {selectedId&&<section id="recovery-detail" data-testid="recovery-detail" className="recovery-detail" aria-busy={detailLoading}>
        <div className="heading-actions"><h3>隔离操作诊断</h3><button className="secondary" data-testid="close-recovery-detail" onClick={clearDetail}>关闭详情</button></div>
        <p className="source-meta">操作 ID：{selectedId}</p>
        {detailLoading&&<p role="status">正在读取操作账本、来源与审批状态…</p>}
        {detailError&&<p role="alert" className="form-error" data-testid="recovery-detail-error">{detailError}</p>}
        {detailError&&<button className="secondary" onClick={()=>void openDetail(selectedId)}>重新读取详情</button>}
        {detail&&<>
          <div className="heading-actions"><strong>{detail.request_title||detail.request_id}</strong>
            <button className="secondary" data-testid="recovery-open-request" disabled={workflowBusy}
              onClick={()=>{const id=detail.request_id;clearDetail();onOpenRequest(id);}}>打开采购需求</button></div>
          <dl className="binding-grid"><dt>隔离开始时间</dt><dd>{when(detail.hold.created_at)}</dd>
            <dt>审批快照哈希</dt><dd>{detail.snapshot_hash}</dd><dt>执行载荷哈希</dt><dd>{detail.payload_sha256}</dd>
            <dt>本次诊断账本哈希</dt><dd data-testid="recovery-ledger-hash">{detail.ledger_sha256}</dd></dl>
          <h3>原始来源完整性</h3>
          {!detail.sources.length&&<p>未报告关联来源，不能据此认定来源完整。</p>}
          {detail.sources.map((source,index)=><article key={`${source.id||"missing"}-${index}`} className="recovery-source" data-testid="recovery-source">
            <strong>{source.filename}</strong> <span className={`badge ${source.integrity==="verified"?"ok":"bad"}`}>{integrityLabels[source.integrity]}</span>
            <p className="source-meta">来源 {source.id||"绑定缺失"} · 报价 {source.quote_id||"绑定缺失"} · 版本 {source.quote_version??"未知"}<br/>SHA-256：{source.sha256||"未报告"}</p>
          </article>)}
          <h3>原审批诊断</h3>
          {detail.approval?<dl className="binding-grid" data-testid="recovery-approval"><dt>审批 ID / 状态</dt><dd>{detail.approval.id} / {detail.approval.status}</dd>
            <dt>审批到期时间</dt><dd>{when(detail.approval.expires_at)} · {detail.approval.unexpired?"未到期":"已到期或无法核验"}</dd>
            <dt>当前权限已核验 / 仍有效</dt><dd>{yesNo(detail.approval.authority_evaluated)} / {yesNo(detail.approval.authority_current)}</dd>
            <dt>审批与快照匹配</dt><dd>{yesNo(detail.approval.snapshot_matches)}</dd></dl>:<p data-testid="recovery-approval-missing">未找到原审批记录。</p>}
          <p>审批记录存在或匹配，也不代表恢复后可重新执行。新登录身份不能恢复历史审批权限。</p>
          {!!detail.diagnostics.length&&<ul className="recovery-diagnostics" data-testid="recovery-diagnostics">{detail.diagnostics.map((diagnostic,index)=><li key={index}>{explain(diagnostic)}</li>)}</ul>}
          <details><summary>查看原始预期字段</summary><dl className="binding-grid">{Object.entries(detail.expected).map(([key,item])=><Field key={key} name={key} item={item}/>)}</dl></details>
          <div className="recovery-reconciliation">
            <h3>ERP 只读核对</h3><p>显式读取远端并对照本次账本。不会保存核对回执、写入 ERP 或修改隔离状态。</p>
            <div className="heading-actions"><button className="secondary" data-testid="reconcile-recovery" disabled={reconciling}
              onClick={()=>void reconcile()}>{reconciling?"正在只读核对…":"开始只读核对"}</button>
              {reconciling&&<button className="secondary" data-testid="cancel-recovery-reconciliation" onClick={()=>{
                context.current?.reader.cancel("reconciliation");setReconciling(false);setResult(null);setReconcileError("");
                setNotice("已取消本页等待，未得到核对结论。远端结果仍不确定，禁止重放。");
              }}>取消等待</button>}</div>
            {notice&&<p role="status">{notice}</p>}
            {reconcileError&&<p role="alert" className="form-error" data-testid="recovery-reconcile-error">{reconcileError}</p>}
            {result&&<div data-testid="recovery-reconciliation-result" role="status">
              <p className="stale-notice" data-testid="recovery-result-status">{reconciliationMessage(result.status)}</p>
              <p>{result.simulated?"模拟 ERP 结果，未验证真实 ERP。":"ERP 只读诊断结果。"} 核对时间：{when(result.verified_at)}</p>
              <dl className="binding-grid"><dt>远端 ID</dt><dd>{result.remote_id||"未找到或未报告"}</dd>
                <dt>尝试访问远端网络</dt><dd>{yesNo(result.network_attempted)}</dd><dt>与原快照一致</dt><dd>{yesNo(result.matches_snapshot)}</dd>
                <dt>草稿状态已核验</dt><dd>{yesNo(result.draft_verified)}</dd><dt>外部写入 / 重放权限</dt><dd>未尝试外部写入 / 禁止重放</dd>
                <dt>核对账本哈希</dt><dd>{result.ledger_sha256}</dd></dl>
              {result.reason&&<p className="form-error">服务端原因：{explain(result.reason)}</p>}
              <h3>预期与远端观测</h3>
              {result.observed===null&&<p>没有可供比较的远端字段；预期值不能替代远端观测。</p>}
              <div className="table-scroll"><table><thead><tr><th>字段</th><th>原快照预期</th><th>远端观测</th></tr></thead><tbody>
                {[...new Set([...Object.keys(result.expected),...Object.keys(result.observed||{})])].map(key=><tr key={key}>
                  <td>{fieldLabels[key]||key}</td><td>{value(result.expected[key])}</td><td>{value(result.observed?.[key])}</td></tr>)}
              </tbody></table></div>
              {!!result.differences.length&&<><h3>字段差异</h3><ul className="recovery-diagnostics" data-testid="recovery-differences">{result.differences.map((difference,index)=><li key={index}>
                {fieldLabels[difference.field]||difference.field}：预期 {value(difference.expected)}；观测 {value(difference.observed)}。{explain(difference.reason)}
              </li>)}</ul></>}
            </div>}
          </div>
        </>}
      </section>}
    </div>
  </section>;
}

function Field({name,item}:{name:string;item:unknown}) {return <><dt>{fieldLabels[name]||name}</dt><dd>{value(item)}</dd></>;}
