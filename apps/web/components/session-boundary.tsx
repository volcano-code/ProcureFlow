"use client";
import {useEffect,useRef,useState,type ReactNode} from "react";
import {API,api,type Identity,type Capabilities,SessionScope} from "@/lib/api";
import {requestJSON} from "@/lib/transport.mjs";

type Config={mode:"demo"|"private"|"pilot";login_method:"invite"|"static_bearer";session_ttl_seconds:number|null};
type Login={token:string;expires_at:string;identity:Identity};
type Session={scope:SessionScope;identity:Identity;capabilities:Capabilities};
export type AuthenticatedProps={token:SessionScope;me:Identity;cap:Capabilities;authControls:ReactNode};
const failure=(error:unknown)=>error instanceof Error?error.message:String(error);

/** Authentication owns the entire workbench lifetime, including nested dialogs and evidence. */
export default function SessionBoundary({children}:{children:(props:AuthenticatedProps)=>ReactNode}) {
  const [config,setConfig]=useState<Config|null>(null),[session,setSession]=useState<Session|null>(null);
  const [busy,setBusy]=useState(false),[notice,setNotice]=useState("");
  const [configFailed,setConfigFailed]=useState(false),[configAttempt,setConfigAttempt]=useState(0);
  const attempt=useRef(0),current=useRef<SessionScope|null>(null),pending=useRef<AbortController|null>(null);
  const loginLock=useRef(false);
  const clear=(reason:"cancelled"|"replaced"|"logout"|"navigation")=>{
    ++attempt.current;pending.current?.abort();pending.current=null;
    const old=current.current;current.current=null;old?.invalidate(reason);
    loginLock.current=false;setSession(null);setBusy(false);
  };
  useEffect(()=>{
    const controller=new AbortController();setConfigFailed(false);
    void requestJSON<Config>(API,"","/auth/config",{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)])}).then(value=>{
      if(!["demo","private","pilot"].includes(value.mode)||value.login_method!==(value.mode==="pilot"?"invite":"static_bearer"))
        throw new Error("服务端未报告受支持的登录模式");
      setConfig(value);
    }).catch(error=>{if(!controller.signal.aborted){setConfigFailed(true);setNotice(`无法读取登录模式：${failure(error)}。请重试，尚未发送任何凭据。`);}});
    return()=>controller.abort();
  },[configAttempt]);
  useEffect(()=>{
    // BFCache/history restoration cannot resurrect an in-memory authenticated view.
    const navigate=()=>{clear("navigation");setNotice("页面导航已清除本地会话，请重新登录。未自动重试任何业务操作。");};
    const restore=(event:PageTransitionEvent)=>{if(event.persisted)navigate();};
    window.addEventListener("pagehide",navigate);window.addEventListener("popstate",navigate);window.addEventListener("pageshow",restore);
    return()=>{window.removeEventListener("pagehide",navigate);window.removeEventListener("popstate",navigate);window.removeEventListener("pageshow",restore);
      ++attempt.current;pending.current?.abort();current.current?.invalidate("navigation");};
  },[]);
  useEffect(()=>{
    if(!session||session.capabilities.mode!=="pilot")return;
    const controller=new AbortController();let reading=false;
    const check=()=>{
      if(reading||controller.signal.aborted||!session.scope.active)return;
      reading=true;
      // Read-only revalidation also covers an empty workbench with no active audit stream.
      void api<Identity>("/me",session.scope,"GET",undefined,controller.signal)
        .catch(()=>{}).finally(()=>{reading=false;});
    };
    const timer=setInterval(check,15000);window.addEventListener("focus",check);
    return()=>{clearInterval(timer);window.removeEventListener("focus",check);controller.abort();};
  },[session]);
  const login=async(credential:string)=>{
    if(!config||loginLock.current||!credential)return;
    clear("replaced");const ticket=attempt.current;
    loginLock.current=true;setBusy(true);setNotice("");
    const controller=new AbortController();pending.current=controller;
    let scope:SessionScope|null=null;
    try {
      const exchanged=config.mode==="pilot"?await requestJSON<Login>(API,"","/auth/login",{
        method:"POST",body:{credential},signal:controller.signal}):null;
      if(ticket!==attempt.current||controller.signal.aborted)return;
      if(exchanged&&(!exchanged.expires_at||Date.parse(exchanged.expires_at)<=Date.now()))throw new Error("服务端返回的会话已过期");
      scope=new SessionScope(exchanged?exchanged.token:credential,{expiresAt:exchanged?.expires_at||null,onInvalidated:reason=>{
        if(current.current!==scope)return;
        ++attempt.current;pending.current?.abort();pending.current=null;current.current=null;
        loginLock.current=false;setSession(null);setBusy(false);
        setNotice(reason==="expired"?"会话已过期，请使用新的邀请凭据重新登录。未自动重试任何业务操作。"
          :"会话已失效或被撤销，请重新登录。已清除本地工作台；未自动重试任何业务操作。");
      }});
      current.current=scope;
      const [identity,capabilities]=await Promise.all([api<Identity>("/me",scope),api<Capabilities>("/capabilities",scope)]);
      scope.assertCurrent();
      if(ticket!==attempt.current)return;
      if(capabilities.mode!==config.mode)throw new Error("登录模式已变化，请刷新页面重新核验");
      if(exchanged&&(identity.user_id!==exchanged.identity.user_id||identity.tenant_id!==exchanged.identity.tenant_id||identity.role!==exchanged.identity.role))
        throw new Error("会话身份核验失败");
      setSession({scope,identity,capabilities});
    } catch(error) {
      if(ticket===attempt.current){current.current=null;scope?.invalidate("cancelled");setSession(null);
        setNotice(`登录失败：${failure(error)}${config.mode==="pilot"?"。邀请凭据可能已被使用；请向试点管理员确认或获取新邀请。":""}`);}
    } finally {if(ticket===attempt.current){loginLock.current=false;setBusy(false);pending.current=null;}}
  };
  const logout=async()=>{
    const active=session;if(!active)return;
    let credential:string;
    try {credential=active.scope.authorization();}catch{return;}
    clear("logout");const ticket=attempt.current;
    if(config?.mode!=="pilot"){setNotice("已退出本地会话。静态令牌仍由服务端配置管理；此操作不撤销令牌。");return;}
    setNotice("已清除本地会话，正在确认服务端撤销。未自动重试任何业务操作。");
    try {
      await requestJSON(API,credential,"/auth/logout",{method:"POST",signal:AbortSignal.timeout(10000)});
      if(ticket===attempt.current)setNotice("已退出，会话已在服务端撤销。未自动重试任何业务操作。");
    } catch(error) {
      if(ticket===attempt.current)setNotice(`已退出本地会话，但无法确认服务端撤销：${failure(error)}。原会话可能仍有效至到期；请联系试点管理员撤销。`);
    }
  };
  const demoIdentity=session?.identity.tenant_id==="demo"&&
    session.identity.user_id===({buyer:"buyer-01",approver:"approver-01",auditor:"auditor-01"}[session.identity.role])
      ?`demo-${session.identity.role}`:"custom";
  const controls=<>
    {(!session||config?.mode!=="pilot")&&<form className="identity" onSubmit={event=>{
      event.preventDefault();const form=event.currentTarget;const value=String(new FormData(form).get("credential")||"").trim();
      form.reset();void login(value);
    }}>
      <input aria-label={config?.mode==="pilot"?"邀请凭据":"访问令牌"} data-testid="login-credential" name="credential" type="password"
        autoComplete="off" spellCheck={false} placeholder={config?.mode==="pilot"?"一次性试点邀请凭据":"服务端分配的访问令牌"} required disabled={!config||busy}/>
      <button className="secondary" data-testid="login-submit" disabled={!config||busy}>{busy?"正在登录…":"登录"}</button>
      {busy&&<button type="button" className="secondary" data-testid="cancel-login" onClick={()=>{
        clear("cancelled");setNotice("已取消登录。邀请可能已在服务端兑换；若再次登录失败，请获取新邀请。未自动重试。");
      }}>取消登录</button>}
    </form>}
    <div className="identity" data-testid="session-identity"><span data-testid="identity">{session?.identity.role||"未登录"}</span>
      {session&&<><span data-testid="identity-user">用户 {session.identity.user_id}</span><span data-testid="identity-tenant">租户 {session.identity.tenant_id}</span>
        <span data-testid="session-expiry">{session.scope.expiresAt?`到期 ${new Date(session.scope.expiresAt).toLocaleString()}`:"静态令牌 · 无会话到期时间"}</span>
        <button className="secondary" data-testid="logout" onClick={()=>void logout()}>退出登录</button></>}
    </div>
    {session?.capabilities.demo_samples&&config?.mode==="demo"&&<select aria-label="切换演示身份" disabled={busy}
      value={demoIdentity}
      onChange={event=>void login(event.target.value)}>
      <option value="demo-buyer">采购员</option><option value="demo-approver">独立审批人</option><option value="demo-auditor">审计员</option>
      {demoIdentity==="custom"&&<option value="custom" disabled>当前自定义身份</option>}
    </select>}
  </>;
  return <>{session?children({token:session.scope,me:session.identity,cap:session.capabilities,authControls:controls}):
    <main style={{marginLeft:0}}><header className="topbar" style={{height:"auto",minHeight:74,paddingTop:12,paddingBottom:12}}>{controls}</header>
      <section className="content"><section className="empty panel"><h1>ProcureFlow 采购工作台</h1><h2>登录后开始采购核对</h2>
        <p>{!config?(configFailed?"登录配置无法读取":"正在读取登录配置…"):config.mode==="pilot"
          ?"试点登录：输入管理员提供的一次性邀请凭据。会话仅保留在当前页面内存中；刷新或离开页面后须重新登录。"
          :config.mode==="demo"?"演示环境采用合成数据与独立身份。访问令牌仅保存在当前页面内存中。"
          :"受限私有模式：请输入服务端配置的静态访问令牌。此模式不提供持久会话或令牌撤销。"}</p>
        {config?.mode==="demo"&&<button data-testid="login-demo-buyer" className="primary" disabled={busy} onClick={()=>void login("demo-buyer")}>以演示采购员登录</button>}
        {configFailed&&<button className="secondary" onClick={()=>{setNotice("");setConfigAttempt(value=>value+1);}}>重试登录配置</button>}
      </section></section></main>}
    {notice&&<div role="status" data-testid="session-notice" style={{position:"fixed",bottom:16,left:16,right:16,zIndex:30,background:"var(--white)",border:"1px solid var(--line)",padding:16,color:"var(--red)"}}>{notice}</div>}
  </>;
}
