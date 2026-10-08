"use client";
import type {Credential} from "@/lib/session.mjs";
import {useEffect,useRef,useState,type FormEvent} from "react";
import {api,type Quote,type QuoteValues,type TableImportPreview,type TableImportSelection} from "@/lib/api";
import {TABLE_FIELDS,createImportScope,importExpired,importReadOnly,mappingProblems,selectionForSheet,previewBody,type ImportScope} from "@/lib/table-import.mjs";

type Props={token:Credential;requestId:string;requestVersion:number;disabled:boolean;workflowBusy:boolean;onImported:()=>void};
type DialogProps=Omit<Props,"disabled"|"workflowBusy">&{onClose:()=>void};
const message=(error:unknown)=>error instanceof Error?error.message:String(error);

/** A fresh mounted dialog owns each browser scope. Closing, changing request/identity or navigating
 * away aborts waits and invalidates every outstanding result; mutations are never auto-replayed. */
function TableImportDialog({token,requestId,requestVersion,onImported,onClose}:DialogProps) {
  const dialog=useRef<HTMLDialogElement>(null),scope=useRef<ImportScope|null>(null);
  const closeRef=useRef(onClose);closeRef.current=onClose;
  const [preview,setPreview]=useState<TableImportPreview|null>(null);
  const [selection,setSelection]=useState<TableImportSelection|null>(null);
  const [working,setWorking]=useState<"upload"|"preview"|"confirm"|"read"|null>(null);
  const [error,setError]=useState("");
  const [blocked,setBlocked]=useState(false);
  const [reviewedRevision,setReviewedRevision]=useState<number|null>(null);
  const [acknowledge,setAcknowledge]=useState(false);
  const [quoteId,setQuoteId]=useState<string|null>(null);
  const [now,setNow]=useState(Date.now());
  useEffect(()=>{
    const active=createImportScope();scope.current=active;
    if(dialog.current&&!dialog.current.open)dialog.current.showModal();
    const navigate=()=>{active.dispose();closeRef.current();};
    window.addEventListener("popstate",navigate);
    return()=>{active.dispose();window.removeEventListener("popstate",navigate);};
  },[]);
  useEffect(()=>{setReviewedRevision(null);setAcknowledge(false);},[requestVersion]);
  useEffect(()=>{
    if(!preview||quoteId)return;
    // A preview that expires while open must stop being actionable without a click or rerender.
    const timer=setTimeout(()=>setNow(Date.now()),Math.min(2147483647,Math.max(0,Date.parse(preview.expires_at)-Date.now()+10)));
    return()=>clearTimeout(timer);
  },[preview,quoteId]);
  const expired=!!preview&&importExpired(preview.expires_at,now);
  const archived=preview?.status==="ARCHIVED";
  const readOnly=!!preview&&importReadOnly(preview.status);
  const sheet=preview?.sheets.find(item=>item.name===selection?.sheet);
  const problems=selection?mappingProblems(selection,sheet):[];
  const close=()=>{scope.current?.dispose();onClose();};
  const retain=(saved:TableImportPreview)=>{
    setPreview(saved);setSelection(saved.selection || (saved.sheets[0]?selectionForSheet(saved.sheets[0]):null));
    setReviewedRevision(null);setAcknowledge(false);setNow(Date.now());setQuoteId(saved.quote_id);
  };
  const upload=async(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();
    const form=new FormData(event.currentTarget),file=form.get("file");
    if(!(file instanceof File)||!file.size){setError("请选择非空 CSV 或 XLSX 文件。");return;}
    if(!/\.(csv|xlsx)$/i.test(file.name)){setError("普通表格仅支持 CSV / XLSX；TXT、PDF 和固定键值布局请使用原有上传报价。");return;}
    const active=scope.current,ticket=active?.begin();if(!active||!ticket)return;
    setWorking("upload");setError("");setBlocked(false);
    try {
      const saved=await api<TableImportPreview>(`/requests/${requestId}/table-imports`,token,"POST",form,ticket.signal);
      if(scope.current!==active||!active.current(ticket))return;
      retain(saved);
      if(saved.status==="IMPORTED")onImported();
    } catch(e) {if(scope.current===active&&active.current(ticket))setError(`${message(e)}。尚未自动重试；可重新选择文件。`);}
    finally {active.finish(ticket);if(scope.current===active&&active.current(ticket))setWorking(null);}
  };
  const change=(next:TableImportSelection)=>{
    setSelection(next);setReviewedRevision(null);setAcknowledge(false);
  };
  const map=async()=>{
    if(!preview||readOnly||!selection||working||blocked||expired||problems.length||quoteId)return;
    const active=scope.current,ticket=active?.begin();if(!active||!ticket)return;
    setWorking("preview");setError("");setReviewedRevision(null);setAcknowledge(false);
    try {
      const saved=await api<TableImportPreview>(`/table-imports/${preview.id}/preview`,token,"POST",previewBody(preview.revision,selection),ticket.signal);
      if(scope.current!==active||!active.current(ticket))return;
      retain(saved);setReviewedRevision(saved.revision);
    } catch(e) {
      if(scope.current===active&&active.current(ticket)){
        setBlocked(true);setError(`${message(e)}。映射预览可能已更新；请先回读已保存状态并重新核对，不会自动重试。`);
      }
    } finally {active.finish(ticket);if(scope.current===active&&active.current(ticket))setWorking(null);}
  };
  const read=async()=>{
    if(!preview||working)return;
    const active=scope.current,ticket=active?.begin();if(!active||!ticket)return;
    setWorking("read");setError("");setReviewedRevision(null);setAcknowledge(false);
    try {
      const saved=await api<TableImportPreview>(`/table-imports/${preview.id}`,token,"GET",undefined,ticket.signal);
      if(scope.current!==active||!active.current(ticket))return;
      retain(saved);setBlocked(false);
      if(saved.status==="IMPORTED")onImported();
    } catch(e) {if(scope.current===active&&active.current(ticket)){setBlocked(true);setError(`${message(e)}。无法核验当前状态；禁止导入，请稍后回读或关闭。`);}}
    finally {active.finish(ticket);if(scope.current===active&&active.current(ticket))setWorking(null);}
  };
  const canConfirm=!!preview&&!!selection&&!working&&!blocked&&!expired&&!problems.length&&!quoteId
    &&preview.status==="OPEN"&&preview.can_confirm&&!!preview.values&&reviewedRevision===preview.revision;
  const confirm=async()=>{
    if(!preview||!canConfirm||!acknowledge)return;
    const active=scope.current,ticket=active?.begin();if(!active||!ticket)return;
    setWorking("confirm");setError("");
    try {
      const quote=await api<Quote>(`/table-imports/${preview.id}/confirm`,token,"POST",{expected_revision:preview.revision,acknowledge:true},ticket.signal);
      if(scope.current!==active||!active.current(ticket))return;
      setQuoteId(quote.id);setReviewedRevision(null);setAcknowledge(false);onImported();
    } catch(e) {
      if(scope.current===active&&active.current(ticket)){
        setBlocked(true);setReviewedRevision(null);setAcknowledge(false);
        setError(`${message(e)}。导入结果尚未核验，可能已经创建待核对报价；请回读已保存状态。不会自动重试或确认报价。`);
      }
    } finally {active.finish(ticket);if(scope.current===active&&active.current(ticket))setWorking(null);}
  };
  const columns=[...new Set(sheet?.rows.flatMap(row=>row.cells.map(cell=>cell.column))||[])];
  const header=sheet?.rows.find(row=>row.row===selection?.header_row);
  return <dialog ref={dialog} className="table-import-dialog" data-testid="table-import-dialog" aria-labelledby="table-import-title"
    onCancel={event=>{event.preventDefault();if(working!=="confirm")close();}}>
    <div className="modal-head"><div><h2 id="table-import-title">普通表格 · 映射预览</h2><p>CSV / XLSX · 一次选取一张工作表中的一行报价</p></div>
      <button aria-label="关闭表格导入" disabled={working==="confirm"} onClick={close}>×</button></div>
    <p>先选择工作表、表头、数据行和字段列，再核对来源。导入只创建待核对报价；之后仍须修正字段并单独确认。公式不执行，缺失运费和税价保持未知。</p>
    {error&&<p role="alert" className="form-error" data-testid="table-import-error">{error}</p>}
    {working&&<p role="status" data-testid="table-import-working">{working==="upload"?"正在保存原始表格…":working==="preview"?"正在生成映射预览…":working==="read"?"正在回读持久化状态…":"正在创建待核对报价，请等候回执…"}</p>}
    {!preview&&<form data-testid="table-upload-form" onSubmit={upload}>
      <label className="form-field">普通供应商表格<input name="file" data-testid="table-import-file" type="file" accept=".csv,.xlsx" required disabled={!!working}/></label>
      <p className="form-hint">支持常见中英文表头；候选映射只是建议。最多 2 MiB；不支持扫描件、宏工作簿或任意单位 / 币种换算。</p>
      <div className="form-actions"><button type="button" className="secondary" onClick={close}>取消</button><button className="primary" data-testid="upload-table-preview" disabled={!!working}>上传并查看原始表格</button></div>
    </form>}
    {preview&&<>
      <div className="source-meta table-import-source" data-testid="table-import-source">{preview.filename} · 预览 v{preview.revision} · {preview.id}<br/>原文件 SHA-256：{preview.document_sha256}<br/>到期：{preview.expires_at}</div>
      {quoteId?<div data-testid="table-import-success" role="status" className="table-import-result">
        <h3>表格已导入</h3><p>报价 {quoteId}。本次导入操作只创建待核对报价，不会确认字段、生成审批或 ERP 草稿。报价可能已被其他操作更新；当前状态以回读后的报价列表为准。</p>
        <div className="form-actions"><button className="primary" data-testid="finish-table-import" onClick={close}>返回报价列表</button></div>
      </div>:<>
        {archived?<p role="alert" className="stale-notice" data-testid="table-import-archived">此过期预览已归档，原始文件和映射记录仍保留。请联系操作员取消归档后重新上传并核对映射；取消归档不会恢复导入权限。</p>:expired&&<p role="alert" className="stale-notice">此预览已过期，不能导入。请关闭后重新上传；原报价不会自动改变。</p>}
        <div className="heading-actions table-import-actions"><button className="secondary" data-testid="read-table-import" disabled={!!working} onClick={()=>void read()}>回读已保存状态</button>
          <span className="source-meta">回读会舍弃尚未预览的选择；不会导入或确认报价</span></div>
        {selection&&<>
          <fieldset disabled={!!working||blocked||expired||readOnly} className="policy-fields">
            <div className="form-grid table-import-selectors">
              <label className="form-field">工作表 / Sheet<select data-testid="table-sheet" value={selection.sheet} onChange={event=>{
                const next=preview.sheets.find(item=>item.name===event.target.value);if(next)change(selectionForSheet(next));
              }}>{preview.sheets.map(item=><option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
              <label className="form-field">表头行 / Header row<select data-testid="table-header-row" value={selection.header_row} onChange={event=>{
                const row=Number(event.target.value);change({...selection,header_row:row,row:sheet?.rows.find(item=>item.row>row)?.row??row,mapping:{}});
              }}>{sheet?.rows.map(row=><option key={row.row} value={row.row}>第 {row.row} 行</option>)}</select></label>
              <label className="form-field">报价数据行 / Quote row<select data-testid="table-data-row" value={selection.row} onChange={event=>change({...selection,row:Number(event.target.value)})}>
                {sheet?.rows.filter(row=>row.row>selection.header_row).map(row=><option key={row.row} value={row.row}>第 {row.row} 行 · {row.cells.map(cell=>cell.value??"").filter(Boolean).join(" · ").slice(0,90)}</option>)}
              </select></label>
            </div>
            <h3>字段列映射</h3><p>空白映射表示未知。单位与币种也必须有来源，不能从需求或表头猜测填入。</p>
            <div className="form-grid table-mapping-grid">{TABLE_FIELDS.map(([field,label])=><label className="form-field" key={field}>{label}
              <select data-testid={`table-map-${field}`} value={selection.mapping[field]||""} onChange={event=>change({...selection,mapping:{...selection.mapping,[field]:event.target.value}})}>
                <option value="">不映射，保持未知</option>{columns.map(column=><option key={column} value={column}>{column} · {header?.cells.find(cell=>cell.column===column)?.value||"（空表头）"}</option>)}
              </select></label>)}</div>
          </fieldset>
          <details className="table-import-raw" open><summary>原始单元格 · {selection.sheet} · 高亮第 {selection.row} 行</summary>
            <div className="table-scroll"><table data-testid="table-raw-grid"><thead><tr><th scope="col">行</th>{columns.map(column=><th scope="col" key={column}>{column}</th>)}</tr></thead>
              <tbody>{sheet?.rows.map(row=><tr key={row.row} className={row.row===selection.row?"selected-import-row":row.row===selection.header_row?"import-header-row":""}>
                <th scope="row">{row.row}</th>{columns.map(column=>{const cell=row.cells.find(item=>item.column===column);return <td key={column}><span>{cell?.value??"（空）"}</span>{cell?.formula&&<strong className="unknown">公式，不执行</strong>}<small>{cell?.cell||`${column}${row.row}`}</small></td>;})}</tr>)}</tbody>
            </table></div>
          </details>
          {!!problems.length&&<ul className="violation-list" role="alert">{problems.map(value=><li key={value}>{value}</li>)}</ul>}
          <div className="form-actions"><button className="secondary" data-testid="preview-table-mapping" disabled={!!working||blocked||expired||readOnly||!!problems.length} onClick={()=>void map()}>生成映射预览</button></div>
        </>}
        {preview.values&&<section className="table-import-result" data-testid="table-mapped-preview" aria-labelledby="table-mapped-title">
          <h3 id="table-mapped-title">字段与来源核对 · 预览 v{preview.revision}</h3>
          {!archived&&reviewedRevision!==preview.revision&&<p className="stale-notice" data-testid="table-preview-dirty">选择尚未生成最新预览，或已回读旧状态。以下仅为上次保存结果；请重新生成映射预览。</p>}
          <div className="table-scroll"><table><thead><tr><th>字段</th><th>映射值</th><th>来源与原文</th></tr></thead><tbody>{TABLE_FIELDS.map(([field,label])=>{
            const value=preview.values![field],source=preview.evidence?.[field];return <tr key={field} data-testid={`table-result-${field}`}><th scope="row">{label}</th>
              <td>{value===null||value==="unknown"?<span className="unknown">未知</span>:String(value)}</td><td><span>{source?.text||source?.reason||"没有可用来源，保持未知"}</span>
                <small>{source?.sheet||selection?.sheet}{source?.cell_range?`!${source.cell_range}`:source?.row?` · 第 ${source.row} 行`:""}</small></td></tr>;
          })}</tbody></table></div>
          {!!preview.issues.length&&<ul data-testid="table-import-issues" className="violation-list">{preview.issues.map((issue,index)=><li key={`${index}:${issue}`}>{issue}</li>)}</ul>}
          <p>字段未知、格式异常或公式不会按零计算。导入后可修正；只有正常报价确认和规则校验通过后才能进入审批。</p>
          <label className="ack-label"><input type="checkbox" data-testid="table-import-ack" checked={acknowledge} disabled={!canConfirm} onChange={event=>setAcknowledge(event.target.checked)}/>
            我已核对本次工作表、行、列和来源，确认导入为待核对报价；此操作不确认报价字段</label>
          <div className="form-actions"><button className="secondary" disabled={working==="confirm"} onClick={close}>取消</button>
            <button className="primary" data-testid="confirm-table-import" disabled={!canConfirm||!acknowledge} onClick={()=>void confirm()}>导入为待核对报价</button></div>
        </section>}
      </>}
    </>}
  </dialog>;
}

export default function TableImportButton({disabled,workflowBusy,...props}:Props) {
  const [open,setOpen]=useState(false);
  useEffect(()=>{if(disabled)setOpen(false);},[disabled]);
  // Parent request/identity scope is keyed; freezing the request also dismisses stale controls.
  return <><button className="secondary" data-testid="open-table-import" disabled={disabled||workflowBusy} onClick={()=>setOpen(true)}>导入普通表格</button>
    {open&&!disabled&&<TableImportDialog {...props} onClose={()=>setOpen(false)}/>}</>;
}
