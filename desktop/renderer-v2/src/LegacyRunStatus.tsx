// 为会话输入区提供运行状态、实际模型和上下文控制入口。
// 主要引用方包括 `AgentHome.tsx`。
import {useCallback,useEffect,useId,useRef,useState,type CSSProperties} from "react";
import {useQuery} from "@tanstack/react-query";
import {api} from "./client";
import type {ContextView} from "./generated";
import {useUiLanguage} from "./UiLanguage";

// 滑块的手动选项从 128K 开始，`0` 表示随模型窗口自动选择。已有自定义上限会在下面保留为独立选项。
const minimumContext = 128 * 1024;
const limits = [0,minimumContext,300 * 1024,600 * 1024,1024 * 1024,2000000];
const compactTokens = new Intl.NumberFormat("en", {notation:"compact",maximumFractionDigits:1});
const contextRoles = [["planner","规划者"],["executor","执行者"],["reviewer","测试者"]] as const;
// 将后端请求快照的 `capacity.breakdown` 键合并为界面分类。旧快照缺少细项时使用已经记录的合计。
const contextCategories = [
  ["系统提示词","#7772f2",["system"]],
  ["本轮要求","#f4b745",["task"]],
  ["对话与续聊","#d58e26",["dialogue"]],
  ["任务书与验收条件","#4e9ddb",["agreement"]],
  ["规划交接","#659fdf",["plan"]],
  ["执行交接与核查证据","#537db0",["handoff"]],
  ["资料范围与讨论背景","#8099a4",["resources"]],
  ["事实树选层","#13af87",["memoryFacts"]],
  ["习惯树选层（含项目规则）","#36beab",["memoryHabits"]],
  ["会话私有记忆","#279893",["memoryPrivate"]],
  ["记忆引导","#53a58b",["memoryGuide"]],
  ["总纲与旧规则","#4c9296",["rules"]],
  ["整理后的历史索引","#8376b2",["compactedHistory"]],
  ["工具定义","#a66ac4",["toolDefinitions"]],
  ["工具调用与结果","#9465b4",["toolResults"]],
  ["技能与插件清单","#dd6e9d",["skills","plugins"]],
  ["图片估算","#c56585",["attachments"]],
  ["请求格式与开销","#8b909c",["protocol"]],
  ["输出预留与安全余量","#716e82",["reservedOutput","safetyReserve"]],
] as const;

// 按会话和当前运行读取请求用量。只有实际记录了多个角色，才提供角色筛选。
// `AgentHome.tsx` 提供会话与运行 ID、执行设置及保存回调，工作间挂载的也是同一个控件。
export function LegacyContextButton({id,runId,limit,disabled,onLimitChange,onDetails}:{
  id:string;runId?:string;limit:number;disabled:boolean;onLimitChange:(value:number)=>Promise<unknown>;onDetails:()=>void;
}) {
  const {t}=useUiLanguage();
  const [open,setOpen]=useState(false), [value,setValue]=useState(limit);
  const [roleChoice,setRoleChoice]=useState<{runId:string;role:string}>();
  const contextPath=`/api/v2/conversations/threads/${encodeURIComponent(id)}/context`;
  const params=new URLSearchParams(runId?{run:runId}:{});
  // 默认查询持续提供圆环用量和当前轮的候选记录。会话没有当前运行 ID 时，由最新快照确定轮次。
  const latest=useQuery<ContextView>({
    queryKey:['context-usage',id,runId,''],
    queryFn:()=>api(`${contextPath}?${params}`),
    enabled:!!id,refetchInterval:5000,
  });
  const requestRun=runId||latest.data?.snapshot?.runId;
  // 只按所查看轮次中已读取的快照生成角色选项。离开该轮或记录失效后，回到最近请求。
  const recordedRoles=new Set(latest.data?.snapshots.filter(row=>row.runId===requestRun).map(row=>row.role));
  const roleOptions=contextRoles.filter(([key])=>recordedRoles.has(key));
  const showRoles=roleOptions.length>1;
  const role=showRoles&&roleChoice&&roleChoice.runId===requestRun&&roleOptions.some(([key])=>key===roleChoice.role)
    ? roleChoice.role : "";
  // 选择角色时发出独立查询，固定同一轮次。只在菜单打开时轮询该角色。
  const byRole=useQuery<ContextView>({
    queryKey:['context-usage',id,requestRun,'role',role],
    queryFn:()=>api(`${contextPath}?${new URLSearchParams({run:requestRun||"",role})}`),
    enabled:!!id&&!!requestRun&&!!role&&open,refetchInterval:5000,
  });
  const view=role?byRole:latest;
  const [saving,setSaving]=useState(false), [error,setError]=useState("");
  const root=useRef<HTMLDivElement>(null), trigger=useRef<HTMLButtonElement>(null), panel=useRef<HTMLElement>(null);
  const timer=useRef<ReturnType<typeof setTimeout>|null>(null), panelId=useId();
  const pointer=useRef<number|null>(null), pendingLimit=useRef<number|null>(null);
  const writer=useRef({limit,disabled,onLimitChange});
  writer.current={limit,disabled,onLimitChange};
  useEffect(()=>{if(pendingLimit.current===null)setValue(limit);},[limit]);
  const saveAfterInput=useCallback(()=>{
    if(timer.current)clearTimeout(timer.current);
    timer.current=setTimeout(()=>{
      timer.current=null;
      const next=pendingLimit.current;
      if(next===null||pointer.current!==null)return;
      pendingLimit.current=null;
      const current=writer.current;
      if(current.disabled){setValue(current.limit);return;}
      if(next===current.limit)return;
      setSaving(true);
      current.onLimitChange(next).catch(error=>{
        setValue(writer.current.limit);
        setError(error instanceof Error?error.message:String(error));
      }).finally(()=>setSaving(false));
    },250);
  },[]);
  useEffect(()=>{
    // Let a native drag finish even when the pointer is released outside the input.
    const finish=()=>{
      if(pointer.current===null)return;
      pointer.current=null;
      if(pendingLimit.current!==null)saveAfterInput();
    };
    const release=(event:PointerEvent)=>{if(event.pointerId===pointer.current)finish();};
    window.addEventListener("pointerup",release,true);
    window.addEventListener("pointercancel",release,true);
    window.addEventListener("blur",finish);
    return ()=>{
      window.removeEventListener("pointerup",release,true);
      window.removeEventListener("pointercancel",release,true);
      window.removeEventListener("blur",finish);
      if(timer.current)clearTimeout(timer.current);
    };
  },[saveAfterInput]);
  useEffect(()=>{
    if(!open)return;
    panel.current?.focus();
    // 点击面板外部时收起菜单，焦点随用户点击的目标移动。
    const outside=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setOpen(false);};
    document.addEventListener("pointerdown",outside);
    return ()=>document.removeEventListener("pointerdown",outside);
  },[open]);
  // 保留键盘访问顺序，关闭后重新聚焦打开菜单的按钮。
  const close=()=>{setOpen(false);trigger.current?.focus();};
  const request=view.error?undefined:view.data?.snapshot;
  const requestLabel=!role&&request&&(showRoles||request.role!=="executor")
    ? contextRoles.find(([key])=>key===request.role)?.[1] : undefined;
  const capacity=request?.capacity;
  const windowSize=capacity?.capacity;
  const ratio=windowSize?capacity.estimatedTotal/windowSize:0;
  // 占用文字按实际估算比例显示。模型窗口未知时，分项占比以本次估算总量为基准。
  const percent=(n:number)=>`${(n*100).toFixed(1)}%`;
  const text=windowSize?`估算占用 ${percent(ratio)}，${compactTokens.format(capacity.estimatedTotal)} / ${compactTokens.format(windowSize)} token`:'上下文用量';
  const raw=capacity?.breakdown;
  const breakdown=raw?contextCategories.map(([name,color,keys])=>({
    name,color,amount:keys.reduce<number>((sum,key)=>sum+(raw[key]??0),0),
  })).filter(item=>item.amount>0):capacity?[
    {name:"请求消息（旧记录）",amount:capacity.inputTokens,color:"#e6a129"},
    {name:"工具定义",amount:capacity.toolTokens,color:"#12ae87"},
    {name:"预留与开销",amount:capacity.protocolTokens+capacity.reservedOutput+capacity.safetyReserve,color:"#9a78e5"},
  ].filter(item=>item.amount>0):[];
  const basis=windowSize||capacity?.estimatedTotal||0;
  const options=limits.includes(limit)||limit<minimumContext?limits:[...limits,limit].sort((a,b)=>a-b);
  const maxIndex=options.length-1;
  const index=Math.max(0,options.indexOf(value));
  const capacityLabel=value>=1000000?compactTokens.format(value):Math.round(value/1024)+"K";
  // 鼠标拖动时只更新显示，松手后保存；连续键盘步进仍合并到一次写入。
  const change=(next:number)=>{
    setValue(next);
    setError("");
    pendingLimit.current=next;
    if(pointer.current===null)saveAfterInput();
  };
  return <div className="composer-context" ref={root} onKeyDown={e=>{if(open&&e.key==="Escape"){e.stopPropagation();close();}}}>
    <button ref={trigger} type="button" id="ctxbtn" className="cring" onClick={()=>setOpen(!open)} title={text}
      aria-label="上下文用量" aria-expanded={open} aria-controls={panelId}>
      <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true"><circle className="cring-bg" cx="10" cy="10" r="8"/><circle className="cring-fg" cx="10" cy="10" r="8" strokeDasharray={`${Math.min(1,ratio)*50.265} 50.265`}/></svg>
    </button>
    {open&&<section id={panelId} ref={panel} tabIndex={-1} className="context-popover" role="dialog" aria-label="上下文用量">
      <header><strong>上下文用量</strong><button type="button" onClick={close} aria-label="关闭上下文用量">×</button></header>
      {(showRoles||request)&&<div className="context-request-picker">
        {showRoles&&<select aria-label="查看角色上下文" value={role}
          onChange={e=>setRoleChoice(requestRun&&e.target.value?{runId:requestRun,role:e.target.value}:undefined)}>
          <option value="">最近请求</option>
          {roleOptions.map(([key,label])=><option key={key} value={key}>{label}</option>)}
        </select>}
        {request&&<small>{requestLabel??'最近请求'} · {request.model.name}</small>}
      </div>}
      {windowSize?<><div className="context-total"><b>{percent(ratio)}</b><small>估算占用 {compactTokens.format(capacity.estimatedTotal)} / {compactTokens.format(windowSize)}</small></div>
      </>:<small role={view.error?"alert":"status"}>{view.error?"暂时无法读取用量":view.isFetching?"正在读取用量…":capacity?`估算用量 ${compactTokens.format(capacity.estimatedTotal)}，模型窗口未知`:role?"当前轮次暂无该角色的模型请求":"发送消息后显示用量"}</small>}
      {!!basis&&<><div className="context-usage-bar" role="img" aria-label={text}>{breakdown.map(item=><span key={item.name} style={{background:item.color,width:percent(item.amount/basis)}}/>)}</div>
        <ul>{breakdown.map(item=><li key={item.name}><i aria-hidden="true" style={{background:item.color}}/>{item.name}<span title={`${item.amount.toLocaleString()} 估算 token`}>{item.amount/basis<.001?"<0.1%":percent(item.amount/basis)}</span></li>)}</ul>
        <small>{windowSize?"占当前窗口的比例":"占本次估算用量的比例"}{raw?" · 仅列本次请求中有用量的来源":" · 旧记录未拆分来源"}{capacity&&!capacity.outputReservationKnown?" · 输出预留未知":""}</small>
      </>}
      <div className="context-limit-control">
        <label htmlFor={panelId+"-limit"}>{value?t("{capacity} 上下文",{capacity:capacityLabel}):t("自动 · 按模型窗口")}</label>
        <input id={panelId+"-limit"} type="range" aria-label={t("上下文上限")} aria-valuetext={value?`${value} token`:t("自动")}
          title={t("仅作用于下一轮，实际窗口受模型限制")} min={0} max={maxIndex} value={index}
          style={{"--context-limit-progress":`${index/maxIndex*100}%`} as CSSProperties} disabled={disabled||saving}
          onPointerDown={event=>{if(event.isPrimary&&event.button===0)pointer.current=event.pointerId;}}
          onChange={event=>change(options[Number(event.target.value)])}/>
        <div className="context-limit-scale" aria-hidden="true"><span>{t("自动")}</span><span>{compactTokens.format(options[maxIndex])}</span></div>
        <small>手动最低128K（131072）；API容量更低时按实际容量。估算只作展示，确认触达上限才压缩。</small>
        {error&&<small role="alert">{error}</small>}
        {id&&<button type="button" className="context-details-link" onClick={()=>{close();onDetails();}}>查看详细记录</button>}
      </div>
    </section>}
  </div>;
}

// 按运行标识查实际模型。当前配置可变，显示值要与原运行匹配。
export function RuntimeModelBadge({id,runId}:{id:string;runId?:string}){
  const view=useQuery<ContextView>({queryKey:['context-usage',id],queryFn:()=>api(`/api/v2/conversations/threads/${encodeURIComponent(id)}/context`),enabled:!!id,refetchInterval:5000});
  // 用实际快照的运行 ID 绑定徽标。刚切换运行时，旧快照的模型不会显示在新一轮上。
  const request=view.data?.snapshot;
  if(!request||request.runId!==runId)return null;
  const names:Record<string,string>={planner:'规划者',executor:'执行者',reviewer:'测试者'};
  return <span id="mdl" className="pill" title="来自实际已保留请求，区别于输入区的下一轮模型配置">最近请求 · {names[request.role]||request.role} <b>{request.model.name}</b></span>;
}
