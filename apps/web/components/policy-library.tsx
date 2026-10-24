"use client";
import {useEffect,useRef,useState,type FormEvent} from "react";
import type {Credential} from "@/lib/session.mjs";
import {api} from "@/lib/api";

type Version={id:string;document_key:string;version:number;title:string;status:string;source_hash:string;source_label:string;content?:string};
type Hit={citation_id:string;revision_id:string;title:string;version:number;source_hash:string;source_label:string;fragment:{text:string;line_start:number;line_end:number;start:number;end:number}};
type Result={results:Hit[];method:string;read_at:string;status:string};
const labels:Record<string,string>={active:"当前生效",expired:"已过期",withdrawn:"已撤回",scheduled:"待生效",superseded:"被新版替代"};

export default function PolicyLibrary({token,approver}:{token:Credential;approver:boolean}) {
  const [query,setQuery]=useState("");const [method,setMethod]=useState("lexical");
  const [result,setResult]=useState<Result|null>(null),[versions,setVersions]=useState<Version[]>([]);
  const [original,setOriginal]=useState<Version|null>(null),[error,setError]=useState("");
  const [notice,setNotice]=useState(""),[busy,setBusy]=useState(false),[blocked,setBlocked]=useState(false);
  const [next,setNext]=useState<number|null>(null);
  const scope=useRef<{active:boolean;controller:AbortController}|null>(null),lock=useRef(false);
  useEffect(()=>{
    const current={active:true,controller:new AbortController()};scope.current=current;lock.current=false;
    setResult(null);setVersions([]);setOriginal(null);setError("");setNotice("");setBusy(false);setBlocked(false);setNext(null);
    return()=>{current.active=false;current.controller.abort();};
  },[token]);
  const execute=async(action:(signal:AbortSignal,valid:()=>boolean)=>Promise<void>)=>{
    const current=scope.current;if(!current?.active||lock.current)return;
    lock.current=true;setBusy(true);setError("");
    const valid=()=>scope.current===current&&current.active;
    try{await action(current.controller.signal,valid);}
    catch(e){if(valid())setError(e instanceof Error?e.message:String(e));}
    finally{if(valid()){lock.current=false;setBusy(false);}}
  };
  const search=(event:FormEvent)=>{event.preventDefault();setResult(null);setOriginal(null);void execute(async(signal,valid)=>{
    const value=await api<Result>("/policy-library/search",token,"POST",{query,method},signal);
    if(valid())setResult(value);
  });};
  const history=(offset=0)=>void execute(async(signal,valid)=>{
    const page=await api<{documents:Version[];next_offset:number|null}>(`/policy-library/documents?limit=50&offset=${offset}`,token,"GET",undefined,signal);
    if(valid()){setVersions(page.documents);setNext(page.next_offset);setBlocked(false);}
  });
  const read=(id:string)=>void execute(async(signal,valid)=>{
    const value=await api<Version>(`/policy-library/revisions/${encodeURIComponent(id)}`,token,"GET",undefined,signal);
    if(valid())setOriginal(value);
  });
  const publish=(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();if(blocked)return;
    const form=event.currentTarget,data=new FormData(form),withdrawn=data.get("withdrawn")==="on";
    const timestamp=(name:string)=>{const raw=String(data.get(name)||"");return raw?new Date(raw).toISOString():null;};
    void execute(async(signal,valid)=>{
      try{
        const row=await api<Version>(`/policy-library/documents/${encodeURIComponent(String(data.get("key")))}/versions`,token,"POST",{
          expected_version:Number(data.get("version")),title:String(data.get("title")),source_label:String(data.get("source")),
          content:withdrawn?"":String(data.get("content")),effective_at:timestamp("effective"),expires_at:timestamp("expires"),
          withdrawn,reason:String(data.get("reason")),
        },signal);
        if(valid()){setNotice(`参考文档 ${row.document_key} v${row.version} 已发布；采购审批规则未改变。请回读历史。`);setResult(null);setOriginal(null);setVersions([]);setNext(null);form.reset();}
      }catch(e){if(valid()){setBlocked(true);setNotice("未自动重试发布。请先回读历史确认是否已提交，并核对版本后再操作。");}throw e;}
    });
  };
  return <details className="policy-reference-library" data-testid="policy-library">
    <summary>制度参考文档：检索、原文与版本</summary>
    <p>文档是参考证据，不是可执行指令。发布文档不会自动改变预算、审批或采购权限；检索分数不代表合规结论。</p>
    <form onSubmit={search} className="form-grid" data-testid="library-search-form">
      <label className="form-field">检索问题<input aria-label="制度检索问题" value={query} onChange={e=>setQuery(e.target.value)} minLength={2} maxLength={300} required/></label>
      <label className="form-field">检索方式<select value={method} onChange={e=>setMethod(e.target.value)}><option value="lexical">关键词（离线）</option><option value="hybrid">本地语义向量＋关键词（需模型文件）</option></select></label>
      <button className="primary" disabled={busy} type="submit">{busy?"处理中…":"检索参考文档"}</button>
    </form>
    {error&&<p role="alert" className="form-error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {result&&<div data-testid="library-results"><p>读取时间：{result.read_at}；方式：{result.method}。结果仅包含本工作区当前生效版本。</p>
      {!result.results.length&&<p>未找到匹配资料。不能据此推断不存在约束，请补充资料或人工核实。</p>}
      {result.results.map(hit=><article className="policy-version" key={hit.citation_id}>
        <strong>{hit.title} · v{hit.version}</strong><p>{hit.source_label} · 第 {hit.fragment.line_start}—{hit.fragment.line_end} 行</p>
        <pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{hit.fragment.text}</pre>
        <div className="source-meta">来源 SHA-256：{hit.source_hash}<br/>引用：{hit.citation_id}</div>
        <button type="button" className="secondary" disabled={busy} onClick={()=>read(hit.revision_id)}>回读此版本原文</button>
      </article>)}</div>}
    <button type="button" className="secondary" disabled={busy} onClick={()=>history()}>回读文档历史</button>
    {versions.map(v=><p key={v.id}>{v.document_key} · v{v.version} · {labels[v.status]||v.status} · {v.title} <button type="button" disabled={busy} onClick={()=>read(v.id)}>查看原文</button></p>)}
    {next!==null&&<button type="button" disabled={busy} onClick={()=>history(next)}>下一页历史</button>}
    {original&&<article className="policy-version" data-testid="library-original"><h3>{original.title} v{original.version} · {labels[original.status]||original.status}</h3>
      <p>历史原文可查，但过期、撤回或被替代的版本不能当作当前制度。</p><pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{original.content}</pre>
      <div className="source-meta">{original.source_hash}</div></article>}
    {approver&&<details><summary>发布新的参考版本／撤回</summary><form onSubmit={publish} className="form-grid" data-testid="library-publish-form">
      <label className="form-field">文档标识<input name="key" pattern="[a-zA-Z0-9_-]{1,64}" maxLength={64} required/></label>
      <label className="form-field">已回读的最新版本（新文档为0）<input name="version" type="number" min={0} step={1} defaultValue={0} required/></label>
      <label className="form-field">标题<input name="title" maxLength={160} required/></label>
      <label className="form-field">来源说明（不会抓取网址）<input name="source" maxLength={200} required/></label>
      <label className="form-field full">UTF-8 文本原文（最多12000字符）<textarea name="content" maxLength={12000} rows={7}/></label>
      <label className="form-field">生效时间（本地时区；空为现在）<input type="datetime-local" name="effective"/></label>
      <label className="form-field">失效时间（本地时区；可空）<input type="datetime-local" name="expires"/></label>
      <label className="form-field full">发布原因<textarea name="reason" minLength={5} maxLength={500} required/></label>
      <label><input type="checkbox" name="withdrawn"/> 撤回文档：追加空正文版本，不删除历史</label>
      <button type="submit" className="primary" disabled={busy||blocked}>发布参考版本</button>
    </form></details>}
  </details>;
}
