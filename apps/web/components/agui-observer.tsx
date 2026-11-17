'use client';
import {useEffect,useRef,useState} from 'react';
import {API,type AdviceRun} from '@/lib/api';
import type {Credential} from '@/lib/session.mjs';
import {observeAdvice} from '@/lib/agui.mjs';

type Progress={id:number;kind:string;tool?:string;node?:string};
const labels:Record<string,string>={
  ADVICE_RESERVED:'建议任务已预留',ADVICE_STARTED:'建议已开始',ADVICE_COMPLETED:'建议已完成',
  ADVICE_FAILED:'生成失败，需人工核对',ADVICE_STALE:'来源已变更',ADVICE_INTERRUPTED:'执行中断，不能自动重放',
  AGENT_GRAPH_NODE:'只读图节点',AGENT_TOOL_STARTED:'只读工具开始',AGENT_TOOL_COMPLETED:'只读工具完成',
  AGENT_TOOL_DENIED:'禁止的工具已被拦截',AGENT_MODEL_ATTEMPT_FAILED:'提供方调用失败',
};
const terminal=new Set(['COMPLETED','FAILED','STALE','INTERRUPTED']);
export default function AGUIObserver({run,token}:{run:AdviceRun;token:Credential}) {
  const [state,setState]=useState<{agentStatus:string;humanAction:string;current:boolean}|null>(null);
  const [items,setItems]=useState<Progress[]>([]);
  const [phase,setPhase]=useState('connecting');
  const cursor=useRef(0);
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>|null=null;
    cursor.current=0;setItems([]);setPhase('connecting');setState(null);
    const poll=async()=>{
      if(controller.signal.aborted)return;
      try {
        const result=await observeAdvice(API,token,{runId:run.id,threadId:run.request_id,
          after:cursor.current,signal:controller.signal,onEvent:(event)=>{
            if(controller.signal.aborted)return;
            if(event.type==='CUSTOM'&&event.name==='procureflow.advice.progress'){
              const value=event.value as {auditId:number;kind:string;detail:Record<string,unknown>};
              setItems(old=>old.some(item=>item.id===value.auditId)?old:
                [...old,{id:value.auditId,kind:value.kind,
                  tool:typeof value.detail?.tool==='string'?value.detail.tool:undefined,
                  node:typeof value.detail?.node==='string'?value.detail.node:undefined}].slice(-12));
            }
          }});
        if(controller.signal.aborted)return;
        cursor.current=result.cursor;
        if(result.state){setState(result.state);setPhase('live');}
        if(!result.state||(!terminal.has(result.state.agentStatus)&&result.state.current))
          timer=setTimeout(()=>{void poll();},2200);
      } catch {
        if(controller.signal.aborted)return;
        setPhase('unavailable'); // Never POST /process or reconnect a paid model request.
      }
    };
    void poll();
    return ()=>{controller.abort();if(timer)clearTimeout(timer);};
  },[run.id,run.request_id,token]);
  return <details data-testid="agui-observer" className="agui-observer">
    <summary>AG-UI 只读运行观察 · {phase==='unavailable'?'连接不可用':state?.agentStatus||'连接中'}</summary>
    <p>事件来源为服务端持久化记录；断线后只读回放，绝不自动重发模型调用或采购请求。</p>
    {phase==='unavailable'&&<p role="status">观察连接失败，请手动回读建议状态；不能据此判断模型已停止或未计费。</p>}
    {state&&<p data-testid="agui-human-action">状态：{state.agentStatus} · 当前来源{state.current?'有效':'待重新核对'} · 下一步：{
      state.humanAction==='review_advice'?'人工核对建议':state.humanAction==='manual_reconcile_no_replay'?'人工对账，不自动重试':
        state.humanAction==='buyer_explicit_start_only'?'等待采购员显式执行':'仅观察，不自动执行'}</p>}
    {items.length>0&&<ul data-testid="agui-progress">{items.map(item=><li key={item.id}>
      {labels[item.kind]||'运行元数据'}{item.tool?` · ${item.tool}`:item.node?` · ${item.node}`:''}
    </li>)}</ul>}
  </details>;
}
