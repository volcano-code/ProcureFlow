"use client";
import {useEffect,useRef,useState,type FormEvent,type ReactNode} from "react";
import type {ProcurementRequest,Quote,QuoteLineValues,QuoteScalarField,RequestLine,EvidenceRef} from "@/lib/api";
import {MAX_LINES,FIELD_LABELS,LINE_FIELDS,HEADER_FIELDS,blankQuoteLine,requestLines,quoteLines,requestLineProblems,coverageProblems,requestPayload,quotePayload,lineEvidence} from "@/lib/procurement-lines.mjs";
import {attachEditorLifecycle} from "@/lib/editor-lifecycle.mjs";

/** Each mounted editor owns its form. Navigation and dismissal discard unsaved changes. */
function EditorDialog({title,id,busy,onClose,children}:{title:string;id:string;busy:boolean;onClose:()=>void;children:ReactNode}) {
  const dialog=useRef<HTMLDialogElement>(null),close=useRef(onClose),locked=useRef(busy);close.current=onClose;locked.current=busy;
  useEffect(()=>attachEditorLifecycle(dialog.current,()=>close.current(),()=>locked.current),[]);
  return <dialog ref={dialog} className="procurement-editor" aria-labelledby={id} onCancel={event=>{event.preventDefault();if(!busy)onClose();}}>
    <div className="modal-head"><h2 id={id}>{title}</h2><button type="button" aria-label={id==="request-form-title"?"关闭需求表单":"关闭报价表单"} disabled={busy} onClick={onClose}>×</button></div>{children}
  </dialog>;
}

export function RequestEditor({request,busy,onClose,onSave}:{request:ProcurementRequest|"new";busy:boolean;onClose:()=>void;onSave:(body:Record<string,unknown>)=>Promise<void>}) {
  const original=request==="new"?null:request;
  const [multiItem,setMultiItem]=useState(!!original?.lines?.length);
  const [lines,setLines]=useState<RequestLine[]>(()=>requestLines(original));
  const [error,setError]=useState("");
  const problems=requestLineProblems(lines);
  const change=(index:number,field:keyof RequestLine,value:string)=>setLines(old=>old.map((line,i)=>i===index?{...line,[field]:value}:line));
  const save=async(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();if(busy)return;
    try{const body=requestPayload(new FormData(event.currentTarget),lines,multiItem);setError("");await onSave(body);}
    catch(error){setError(error instanceof Error?error.message:String(error));}
  };
  return <EditorDialog title={request==="new"?"创建采购需求":"修改需求：旧审批将失效"} id="request-form-title" busy={busy} onClose={onClose}>
    <form data-testid="request-form" onSubmit={save}>
      <fieldset className="policy-fields" disabled={busy}>
        <div className="form-grid">
          <label className="form-field">需求名称<input name="title" defaultValue={original?.title??"研发工位支架采购"} maxLength={120} required/></label>
          <label className="form-field">预算<input name="budget" inputMode="decimal" defaultValue={original?.budget??"30000.00"} required/></label>
          <label className="form-field">最长交期<input name="max_delivery_days" type="number" min={1} max={365} defaultValue={original?.max_delivery_days??14} required/></label>
        </div>
        <h3>采购物料 · {lines.length} / {MAX_LINES}</h3><p>整单 CNY；每项 EA。每个型号只出现一次，报价必须完整覆盖全部物料，不能跨供应商拆单。</p>
        <div data-testid="request-line-list">{lines.map((line,index)=><fieldset className="line-editor" key={index} data-testid="request-line">
          <legend>物料 {index+1}</legend><div className="form-grid">
            <label className="form-field">型号<input name={multiItem?`lines.${index}.sku`:"sku"} data-testid={`request-line-${index}-sku`} value={line.sku} maxLength={80} required onChange={event=>change(index,"sku",event.target.value)}/></label>
            <label className="form-field">数量<input name={multiItem?`lines.${index}.quantity`:"quantity"} data-testid={`request-line-${index}-quantity`} inputMode="decimal" value={line.quantity} required onChange={event=>change(index,"quantity",event.target.value)}/></label>
            <label className="form-field">单位<input aria-label={`物料 ${index+1} 单位`} value="EA" readOnly/></label>
          </div>{multiItem&&<button type="button" className="secondary" aria-label={`删除物料 ${index+1}`} disabled={lines.length===1} onClick={()=>{setLines(old=>old.filter((_,i)=>i!==index));}}>删除物料</button>}
        </fieldset>)}</div>
        <button type="button" className="secondary" data-testid="add-request-line" disabled={lines.length>=MAX_LINES} onClick={()=>{setMultiItem(true);setLines(old=>[...old,{sku:"",quantity:"1",uom:"EA"}]);}}>＋ 添加物料</button>
        {multiItem&&<p data-testid="request-multi-mode">多物料需求：{lines.length} 项。整单预算与最长交期适用于所有物料。</p>}
        {!!problems.length&&<ul role="alert" className="violation-list" data-testid="request-line-problems">{problems.map((problem,index)=><li key={index}>{problem}</li>)}</ul>}
      </fieldset>
      {error&&<p role="alert" className="form-error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="primary" disabled={busy||!!problems.length}>保存需求</button></div>
    </form>
  </EditorDialog>;
}

function QuoteInput({field,name,value,onChange}:{field:QuoteScalarField;name:string;value:string|number|null;onChange?:(value:string)=>void}) {
  return <label className="form-field">{FIELD_LABELS[field]}{field==="tax_mode"?<select name={name} value={value??"unknown"} onChange={event=>onChange?.(event.target.value)}>
    <option value="included">含税</option><option value="excluded">未税</option><option value="unknown">未知</option></select>:
    <input name={name} type={field==="delivery_days"?"number":"text"} min={field==="delivery_days"?1:undefined} max={field==="delivery_days"?365:undefined} step={field==="delivery_days"?1:undefined} value={value??""} onChange={event=>onChange?.(event.target.value)} inputMode={["quantity","unit_price","tax_rate","shipping_cost","discount"].includes(field)?"decimal":field==="delivery_days"?"numeric":undefined}/>}
  </label>;
}

export function QuoteEditor({quote,request,busy,onClose,onSave,onEvidence}:{quote:Quote;request:ProcurementRequest;busy:boolean;onClose:()=>void;onSave:(body:Record<string,unknown>,reason:string)=>Promise<void>;onEvidence:(value:EvidenceRef)=>void}) {
  const multiItem=!!quote.values.lines?.length||!!request.lines?.length;
  const [lines,setLines]=useState<QuoteLineValues[]>(()=>quoteLines(quote.values));
  const [headers,setHeaders]=useState({...quote.values});
  const [sourceIndices,setSourceIndices]=useState<(number|null)[]>(()=>quoteLines(quote.values).map((_,index)=>index));
  const [viewedSource,setViewedSource]=useState<EvidenceRef|null>(null);
  const [error,setError]=useState("");
  const problems=multiItem?coverageProblems(requestLines(request),lines):[];
  const duplicate=problems.some(problem=>problem.startsWith("重复型号："));
  const source=(index:number,field:keyof QuoteLineValues)=>sourceIndices[index]===null?{kind:"unknown",reason:"新增物料尚无已保存的来源，请在修改依据中说明。"}:quote.values.lines?.length?lineEvidence(quote.evidence,sourceIndices[index]!,field):quote.evidence[field]||{kind:"unknown"};
  const change=(index:number,field:keyof QuoteLineValues,value:string)=>setLines(old=>old.map((line,i)=>i===index?{...line,[field]:value===""?null:field==="delivery_days"?Number(value):value}:line));
  const save=async(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();if(busy||duplicate)return;
    try{const form=new FormData(event.currentTarget),values=quotePayload(form,multiItem,lines.length);setError("");await onSave(values,String(form.get("reason")??""));}
    catch(error){setError(error instanceof Error?error.message:String(error));}
  };
  return <EditorDialog title={`报价 v${quote.version} · 修改后需重新确认`} id="quote-form-title" busy={busy} onClose={onClose}>
    <form data-testid="quote-form" onSubmit={save}>
      <fieldset className="policy-fields" disabled={busy}><div className="form-grid">{HEADER_FIELDS.map(field=><QuoteInput key={field} field={field} name={field} value={headers[field]} onChange={value=>setHeaders(old=>({...old,[field]:value===""?null:value}))}/>)}</div>
        <p>运费是整份报价的最终含税金额，只计一次。空白字段保持未知；修正和确认不会补全缺失项。</p>
        <div data-testid="quote-line-list">{lines.map((line,index)=><fieldset key={index} className="line-editor" data-testid="quote-edit-line"><legend>报价物料 {index+1}</legend>
          <div className="form-grid">{LINE_FIELDS.map(field=><div key={field}><QuoteInput field={field} name={multiItem?`lines.${index}.${field}`:field} value={line[field]} onChange={value=>change(index,field,value)}/>
            <button type="button" className="field-link" aria-label={`查看物料 ${index+1} ${FIELD_LABELS[field]}来源`} onClick={()=>{const saved=source(index,field);setViewedSource(saved);onEvidence(saved);}}>查看保存的来源</button>
            <small className="source-meta">{source(index,field)?.text||"无可用原文"}</small>
          </div>)}</div>
          {multiItem&&<button type="button" className="secondary" aria-label={`删除报价物料 ${index+1}`} disabled={lines.length===1} onClick={()=>{setLines(old=>old.filter((_,i)=>i!==index));setSourceIndices(old=>old.filter((_,i)=>i!==index));}}>删除物料</button>}
        </fieldset>)}</div>
        {multiItem&&<><button type="button" className="secondary" data-testid="add-quote-line" disabled={lines.length>=MAX_LINES} onClick={()=>{setLines(old=>[...old,blankQuoteLine()]);setSourceIndices(old=>[...old,null]);}}>＋ 添加报价物料</button>
          <p>1–20 项；必须完整覆盖需求中的 {requestLines(request).map(line=>`${line.sku} (${line.quantity} ${line.uom})`).join("、")}。</p>
          {!!problems.length&&<div data-testid="quote-coverage-problems"><p>重复型号须先修正。其他缺失项可保留为待核对版本，但不能形成合规方案：</p><ul role="alert" className="violation-list">{problems.map((problem,index)=><li key={index}>{problem}</li>)}</ul></div>}</>}
        {viewedSource&&<section data-testid="quote-editor-evidence" aria-label="保存版本的字段来源"><h3>保存版本的字段来源</h3><pre>{viewedSource.text||viewedSource.reason||"没有可用原文，保持未知。"}</pre><p className="source-meta">{viewedSource.sheet}{viewedSource.cell_range?`!${viewedSource.cell_range}`:viewedSource.page?` 第 ${viewedSource.page} 页`:viewedSource.line?` 第 ${viewedSource.line} 行`:viewedSource.kind} · {viewedSource.document_sha256}</p></section>}
        <label className="form-field full">修改依据<textarea name="reason" minLength={5} maxLength={500} required/></label>
      </fieldset>
      {error&&<p role="alert" className="form-error">{error}</p>}
      <div className="form-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button><button className="primary" disabled={busy||duplicate}>保存新版本</button></div>
    </form>
  </EditorDialog>;
}
