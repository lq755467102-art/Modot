// 查询待处理权限请求，呈现范围并记录用户的授权决定。
// 主要引用方包括 `AgentHome.tsx` 和 `ComposerPermissions.tsx`。
import type { UiMessage } from "./uiMessages";
import { useUiLanguage } from "./UiLanguage";
import {useState} from 'react';
import {createPortal} from 'react-dom';
import {useConversationDock} from './ConversationDock';
import {useQuery} from '@tanstack/react-query';
import {api, APIError} from './client';
import './repository-import.css';

// 后端权限队列返回原申请。
// `sessionId` 和 `runId` 定位申请的运行，`fingerprint` 将用户的决定绑定到看到的具体动作。
type Request={id:string;fingerprint:string;sessionId:string;runId:string;state:string;reviewer:string;reason:string;action:unknown};
const labels:Partial<Record<string,UiMessage>>={pending:'等待批准',reviewing:'安全审查中',consumed:'已批准并使用',denied:'已拒绝',expired:'已过期',revoked:'已撤销',approved:'已批准'};

function repositoryAction(action: unknown) {
  if (!action || typeof action !== "object") return;
  const value = action as { kind?: unknown; url?: unknown; destination?: unknown; sshFingerprint?: unknown };
  if (value.kind === "repository-import") return value;
}

function repositoryScope(action: unknown) {
  const value = repositoryAction(action);
  if (typeof value?.url !== "string" || typeof value.destination !== "string") return;
  return { url: value.url, destination: value.destination, ssh: /^(?:git@|ssh:\/\/git@)/.test(value.url),
    fingerprint: typeof value.sshFingerprint === "string" ? value.sshFingerprint : undefined };
}

export function permissionPath(owner = "") {
 return owner ? "/api/v2/conversations/threads/" + encodeURIComponent(owner) + "/permissions" : "/api/v2/permissions";
}
export function usePermissionRequests(watch=true, owner=""){
 return useQuery<{mode:string;sandboxMode:string;revision:number;requests:Request[]}>({
   queryKey:['permission-requests',owner],queryFn:()=>api(permissionPath(owner)),
   enabled:watch,refetchInterval:watch?750:false,refetchIntervalInBackground:true});
}
function usePermissionDecision(refetch:()=>Promise<unknown>){
 const { t } = useUiLanguage();
 const [busy,setBusy]=useState(''),[error,setError]=useState('');
 async function decide(row:Request,decision:string){
  setBusy(row.id);setError('');
  try{await api(`/api/v2/permissions/${row.id}`,{decision,fingerprint:row.fingerprint});}
  catch(e){
    setError(repositoryAction(row.action) && e instanceof APIError && e.status === 409
      ? t("这次导入批准已失效，请发起新的导入请求。") : String(e));
  }
  finally{await refetch();setBusy('');}
 }
 return {busy,error,decide};
}
function RequestAction({row,enabled,busy,onDecide,composer=false}:{row:Request;enabled:boolean;busy:boolean;onDecide:(row:Request,decision:string)=>void;composer?:boolean}){
  const { t } = useUiLanguage();
 const detail=useQuery<Request>({queryKey:['permission-request',row.id],queryFn:()=>api(`/api/v2/permissions/${row.id}`),enabled});
 // 详情查询独立于列表轮询，三项身份都匹配后才开放批准，以免列表已经更新而用户确认了旧动作。
 const checked=detail.data?.fingerprint===row.fingerprint && detail.data?.sessionId===row.sessionId && detail.data?.runId===row.runId;
 const scope = checked ? repositoryScope(detail.data?.action) : undefined;
 const hostShell = checked && (detail.data?.action as {kind?:string}|undefined)?.kind === "host-shell";
 return <>
  {composer && <>
    <strong>{t(scope?.ssh ? "核对这次私库导入" : "等待你的安全批准")}</strong>
    <small>{t(scope?.ssh ? "本机 SSH 身份会读取此仓库。批准仅用于这次导入和下方目标文件夹。"
      : "当前动作已等待你的决定。批准只对这一次操作有效。")}</small>
  </>}
  {!(composer && scope?.ssh && row.reason === "等待批准") &&
    <p>{row.reviewer === "human" && (row.reason === "等待批准" || row.reason === "用户批准一次" || row.reason === "用户拒绝") ? t(row.reason) : row.reason}</p>}
  {detail.error&&<p role="alert">{t("具体动作暂不可读取，不能批准")}</p>}
  {hostShell && <p>{t("这条命令将以当前 Windows 用户身份运行，可访问项目外文件和网络。")}</p>}
  {scope ? <dl className="repository-approval-scope">
    <dt>{t("仓库地址")}</dt><dd><code>{scope.url}</code></dd>
    <dt>{t("目标文件夹")}</dt><dd><code>{scope.destination}</code></dd>
    {scope.fingerprint && <><dt>{t("公钥指纹")}</dt><dd><code>{scope.fingerprint}</code></dd></>}
  </dl> : detail.data && <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:220,overflow:'auto'}}>{JSON.stringify(detail.data.action,null,2)}</pre>}
  {row.state==='pending'&&<div className="composer-approval-actions"><button type="button" disabled={busy||!checked||!!detail.error||detail.data?.state!=='pending'} onClick={()=>onDecide(row,'allow')}>{t("批准这一次")}</button><button type="button" disabled={busy} onClick={()=>onDecide(row,'deny')}>{t("拒绝")}</button></div>}
 </>;
}
function RequestDetails({row,busy,onDecide}:{row:Request;busy:boolean;onDecide:(row:Request,decision:string)=>void}){
  const { t } = useUiLanguage();
 const [open,setOpen]=useState(row.state==='pending'||row.state==='reviewing');
 return <details open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
  <summary>{labels[row.state] ? t(labels[row.state]!) : row.state} · {row.sessionId.slice(0,10)} · {row.reviewer==='safety-agent'?t("安全审查"):t("用户")}</summary>
  <RequestAction row={row} enabled={open} busy={busy} onDecide={onDecide}/>
 </details>;
}
export function ComposerApproval({row,refetch}:{row:Request;refetch:()=>Promise<unknown>}){
  const { t } = useUiLanguage();
 const {busy,error,decide}=usePermissionDecision(refetch);
 const dock=useConversationDock();
 const content=<section className="composer-question composer-approval" role="group" aria-label={t("安全批准请求")} aria-live="polite">
  <RequestAction row={row} enabled busy={!!busy} onDecide={decide} composer/>
  {error&&<p role="alert">{error}</p>}
 </section>;
 return dock?.owner===row.sessionId ? createPortal(content,dock.approval) : content;
}
export function PermissionReview(){
  const { t } = useUiLanguage();
 const data=usePermissionRequests();
 const {busy,error,decide}=usePermissionDecision(data.refetch);
 return <div className="permission-review">
  {data.error&&<p role="alert">{t("批准记录暂不可读取")}</p>}{error&&<p role="alert">{error}</p>}
  {!data.isPending&&data.data?.requests?.length===0&&<p>{t("暂无批准请求")}</p>}
  {data.data?.requests?.map((row:Request)=><RequestDetails key={row.id} row={row} busy={!!busy} onDecide={(r,d)=>void decide(r,d)}/>)}
 </div>;
}
