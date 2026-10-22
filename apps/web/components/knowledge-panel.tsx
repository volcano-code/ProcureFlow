"use client";
import {useCallback,useEffect,useRef,useState,type FormEvent} from "react";
import {api,type Credential} from "@/lib/api";
import {citationPath,latestDocumentVersion} from "@/lib/knowledge.mjs";

type Version={id:string;document_key:string;version:number;latest_version:number;title:string;state:string;
  current:boolean;content_hash:string;effective_at:string|null;expires_at:string|null};
type Hit={citation_id:string;title:string;version:number;text:string;line_start:number;line_end:number;content_hash:string};
type Results={corpus_hash:string;scope:string;status:string;method:string;hits:Hit[]};
type Scope={active:boolean;controller:AbortController};

export default function KnowledgePanel({token,approver,workflowBusy}:{token:Credential;approver:boolean;workflowBusy:boolean}) {
  const [versions,setVersions]=useState<Version[]>([]),[result,setResult]=useState<Results|null>(null);
  const [error,setError]=useState(""),[notice,setNotice]=useState(""),[busy,setBusy]=useState(false);
  const [text,setText]=useState(""),[expanded,setExpanded]=useState(false);
  const scope=useRef<Scope|null>(null),epoch=useRef(0),locked=useRef(false);
  const reload=useCallback(async(active:Scope)=>{
    const data=await api<Version[]>("/knowledge/documents",token,"GET",undefined,active.controller.signal);
    if(active.active&&scope.current===active)setVersions(data);
  },[token]);
  useEffect(()=>{
    const active={active:true,controller:new AbortController()};scope.current=active;
    void reload(active).catch(e=>{if(active.active)setError(e instanceof Error?e.message:String(e));});
    return()=>{active.active=false;active.controller.abort();++epoch.current;};
  },[reload]);
  const run=async(action:(active:Scope,ticket:number)=>Promise<void>)=>{
    const active=scope.current;if(!active?.active||locked.current||workflowBusy)return;
    locked.current=true;setBusy(true);setError("");setNotice("");const ticket=++epoch.current;
    try{await action(active,ticket);}catch(e){
      if(active.active&&scope.current===active){setResult(null);setError(`${e instanceof Error?e.message:String(e)}。未自动重试，请刷新后核对。`);}
    }finally{locked.current=false;if(active.active&&scope.current===active)setBusy(false);}
  };
  const search=(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();const q=String(new FormData(event.currentTarget).get("query")||"");
    void run(async(active,ticket)=>{
      const data=await api<Results>(`/knowledge/search?${new URLSearchParams({q})}`,token,"GET",undefined,active.controller.signal);
      if(active.active&&scope.current===active&&ticket===epoch.current)setResult(data);
    });
  };
  const create=(event:FormEvent<HTMLFormElement>)=>{
    event.preventDefault();const values=new FormData(event.currentTarget);
    const document_key=String(values.get("document_key")||"");
    void run(async(active)=>{
      await api("/knowledge/versions",token,"POST",{document_key,expected_version:latestDocumentVersion(versions,document_key),
        title:String(values.get("title")||""),scope:"procurement",text},active.controller.signal);
      if(!active.active)return;setText("");setResult(null);await reload(active);
      if(active.active)setNotice("新版本已保存为草稿，尚未生效。请核对后单独发布。");
    });
  };
  const transition=(version:Version,action:"publish"|"retire")=>{
    if(!window.confirm(action==="publish"?`发布 ${version.title} v${version.version}？这不会修改采购执行规则。`:`撤回 ${version.title} v${version.version}？旧版本不会自动恢复。`))return;
    void run(async(active)=>{
      await api(`/knowledge/versions/${version.id}/${action}`,token,"POST",{expected_document_version:version.latest_version},active.controller.signal);
      if(!active.active)return;setResult(null);await reload(active);
      if(active.active)setNotice("文档状态已更新。旧检索引用和相关只读建议需重新核验；采购执行规则未改变。");
    });
  };
  return <section className="panel quote-panel" data-testid="knowledge-panel" aria-busy={busy}>
    <div className="section-head"><div><h2>制度文档与证据检索</h2><p>查询已发布且当前生效的原文。检索结果仅供核对，不自动改变采购策略或审批权限。</p></div>
      <button className="secondary" disabled={busy||workflowBusy} onClick={()=>setExpanded(!expanded)}>{expanded?"收起文档管理":"查看文档版本"}</button></div>
    <div className="decision-content">
      <form onSubmit={search} style={{display:"flex",gap:12,flexWrap:"wrap"}}>
        <label style={{flex:"1 1 240px"}}>查找制度条款<input name="query" data-testid="knowledge-query" required maxLength={256} placeholder="例如：运费、交期、供应商准入" style={{width:"100%"}}/></label>
        <button type="submit" className="primary" data-testid="knowledge-search" disabled={busy||workflowBusy}>检索原文</button>
      </form>
      <p className="source-meta">本地关键词＋字符 TF-IDF 检索，不调用付费模型；不保证理解同义改写或判断制度冲突。</p>
      {error&&<p role="alert" className="form-error" data-testid="knowledge-error">{error}</p>}
      {notice&&<p role="status" data-testid="knowledge-notice">{notice}</p>}
      {result&&<div data-testid="knowledge-results">
        {!result.hits.length&&<p>{result.status==="no_effective_documents"?"当前没有适用的已发布文档；不能据此声称没有采购限制。":"没有找到足够相关的片段，请换用制度中的关键词。"}</p>}
        {result.hits.map(hit=><article key={hit.citation_id} style={{borderTop:"1px solid var(--line)",paddingTop:12,marginTop:12}}>
          <strong>{hit.title} · v{hit.version} · 行 {hit.line_start}–{hit.line_end}</strong>
          <pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{hit.text}</pre>
          <p className="source-meta" style={{overflowWrap:"anywhere"}}>原文 SHA-256：{hit.content_hash}</p>
          <button className="secondary" data-testid="knowledge-verify" disabled={busy||workflowBusy} onClick={()=>void run(async(active)=>{
            await api(citationPath(hit.citation_id,result.corpus_hash,result.scope),token,"GET",undefined,active.controller.signal);
            if(active.active)setNotice("已重新核对当前文档版本与原文定位。引用存在不等于结论正确。");
          })}>重新核验引用</button>
        </article>)}
      </div>}
      {expanded&&<div data-testid="knowledge-management">
        <button className="secondary" disabled={busy||workflowBusy} onClick={()=>void run(async(active)=>{setResult(null);await reload(active);})}>刷新文档历史</button>
        {!versions.length&&<p>暂无可见文档。审批人可先创建草稿，再明确发布。</p>}
        {versions.map(v=><div key={v.id} data-testid={`knowledge-version-${v.id}`} style={{padding:"12px 0",borderBottom:"1px solid var(--line)",overflowWrap:"anywhere"}}>
          <strong>{v.title} · {v.document_key} · v{v.version}</strong> <span className="badge">{v.current?"当前生效":v.state==="DRAFT"?"未发布":v.state==="RETIRED"?"已撤回":"未来／已过期／已替代"}</span>
          {approver&&v.state==="DRAFT"&&v.version===v.latest_version&&<button className="secondary" disabled={busy||workflowBusy} data-testid="knowledge-publish" onClick={()=>transition(v,"publish")}>发布此版本</button>}
          {approver&&v.state==="PUBLISHED"&&<button className="secondary" disabled={busy||workflowBusy} data-testid="knowledge-retire" onClick={()=>transition(v,"retire")}>撤回此版本</button>}
        </div>)}
        {approver&&<form onSubmit={create} data-testid="knowledge-create" style={{display:"grid",gap:12,marginTop:16}}>
          <h3>追加文档版本</h3><p>同一文档使用相同标识。保存只创建草稿，不会自动发布或覆盖已有正文。</p>
          <label>文档标识<input required name="document_key" pattern="[a-z][a-z0-9_-]{0,79}" maxLength={80} placeholder="freight-policy"/></label>
          <label>标题<input required name="title" maxLength={160}/></label>
          <label>载入本地 TXT／Markdown<input type="file" accept=".txt,.md,text/plain,text/markdown" disabled={busy} onChange={event=>{
            const file=event.currentTarget.files?.[0],active=scope.current;if(!file||!active)return;
            if(file.size>64000){setError("文件超过64000字节，请拆分文档。");return;}
            const ticket=++epoch.current;
            void file.arrayBuffer().then(buffer=>{const value=new TextDecoder("utf-8",{fatal:true}).decode(buffer);
              if(active.active&&scope.current===active&&ticket===epoch.current)setText(value);
            }).catch(()=>{if(active.active)setError("文件不是有效UTF-8文本。");});
          }}/></label>
          <label>原文<textarea required name="text" data-testid="knowledge-text" value={text} maxLength={32000} rows={7} style={{width:"100%"}} onChange={event=>{++epoch.current;setText(event.target.value);}}/></label>
          <button className="primary" type="submit" data-testid="knowledge-save" disabled={busy||workflowBusy}>保存未发布草稿</button>
        </form>}
      </div>}
    </div>
  </section>;
}
