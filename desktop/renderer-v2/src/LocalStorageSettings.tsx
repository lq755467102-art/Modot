// 查看本机存储、发布备份与迁移状态，并提供受确认控制的操作入口。
// 主要引用方包括 `Settings.tsx`。
import {useRef, useState} from "react";
import {useQuery, useQueryClient} from "@tanstack/react-query";
import {api, queryString} from "./client";
import {Dialog} from "./Dialog";
import type {Conversation} from "./generated";
import {UiIcon} from "./UiIcon";
import {useUiLanguage} from "./UiLanguage";

const categories = {
  conversations:["会话","未归档会话的正文与运行记录。"],
  agents:["子 Agent","子 Agent 的独立执行记录和子任务元数据。"],
  archives:["已归档会话","归档仍占用空间。恢复后，相关记录归回会话分类。"],
  evidence:["附件与证据","附件、成果、留存的工具输出和代码审查证据。"],
  worktrees:["托管工作树","Modot 登记的对话、子任务和工程工作树副本。"],
  other:["缓存与其他","本机索引、记忆、配置和其他应用数据。"],
} as const;
type Category = keyof typeof categories;
// `local_storage.py` 提供的一次本机盘点。
// 计数按字节汇总，`scannedAt` 为 Unix 秒；`partial` 为真时只覆盖已扫描的内容。
type Inventory = {
  categories:{id:Category;bytes:number;files:number}[];
  totalBytes:number; scannedAt:number; dataRoot:string; partial:boolean; skipped:number; reason:string|null;
  worktrees:{name:string;path:string;bytes:number;available:boolean;conversations:string[]}[];
};

function size(bytes:number){
  if(bytes<1024)return bytes.toLocaleString()+" B";
  const order=Math.min(4,Math.floor(Math.log(bytes)/Math.log(1024)));
  return (bytes/1024**order).toLocaleString(undefined,{maximumFractionDigits:1})+" "+["B","KiB","MiB","GiB","TiB"][order];
}

export function LocalStorageSettings({onWorktrees, worktreesOnly=false, focusItem}: {onWorktrees:()=>void;worktreesOnly?:boolean;focusItem?:string}){
  const client=useQueryClient();
  const {t}=useUiLanguage();
  const [selected,setSelected]=useState<Category>();
  const [filter,setFilter]=useState("");
  const [action,setAction]=useState<{id:string;kind:"archive"|"delete"}>();
  const [deleteTarget,setDeleteTarget]=useState<Conversation>();
  const [notice,setNotice]=useState("");
  const [error,setError]=useState("");
  const archiveHeading=useRef<HTMLHeadingElement>(null);
  const inventory=useQuery({
    queryKey:["local-storage"],queryFn:()=>api<Inventory>("/api/v2/local-storage"),retry:false,staleTime:10_000,gcTime:60_000,
  });
  const conversations=useQuery({
    queryKey:["conversations"],queryFn:()=>api<{threads:Conversation[]}>("/api/v2/conversations/threads"),
    enabled:!worktreesOnly,
  });
  const archived=conversations.data?.threads.filter(row=>row.archived&&!row.subtaskOf) ?? [];
  const needle=filter.trim().toLocaleLowerCase();
  const matching=archived.filter(row=>`${row.title} ${row.projectName||""}`.toLocaleLowerCase().includes(needle));
  async function updateArchive(row:Conversation, kind:"archive"|"delete"){
    if(action)return;
    setAction({id:row.id,kind});setError("");setNotice("");
    try{
      await api("/api/v2/conversations/threads/"+queryString(row.id)+"/"+kind,
        kind==="delete"?{confirm:true}:{archived:false},true);
      client.setQueryData<{threads:Conversation[]}>(["conversations"],old=>old&&({
        ...old,threads:kind==="delete"?old.threads.filter(item=>item.id!==row.id):
          old.threads.map(item=>item.id===row.id?{...item,archived:false}:item),
      }));
      const title=row.title||t("未命名会话");
      setNotice(kind==="delete"?t("已删除「{title}」。",{title}):t("已恢复「{title}」。",{title}));
      setDeleteTarget(undefined);
      requestAnimationFrame(()=>archiveHeading.current?.focus({preventScroll:true}));
      // Let an existing scan finish before taking the post-restore snapshot.
      if(client.getQueryState(["local-storage"])?.fetchStatus==="fetching")
        await inventory.refetch({cancelRefetch:false});
      await inventory.refetch({cancelRefetch:false});
    }catch(e){setError((e as Error).message);}
    finally{
      void client.invalidateQueries({queryKey:["conversations"]});
      setAction(undefined);
    }
  }
  function closeDelete(){if(!action){setDeleteTarget(undefined);setError("");}}
  const current=inventory.data?.categories.find(item=>item.id===selected);
  const trees=inventory.data?.worktrees??[];
  return <>
    {inventory.error&&<p role="alert" className="settings-status">读取存储失败。{inventory.error.message} {inventory.data&&"仍显示上次统计。"}</p>}
    {worktreesOnly?<div className="settings-card" data-setting-item="worktree" data-highlight={focusItem==="worktree"||undefined}>
      <h3>托管工作树</h3>
      {inventory.isPending?<p role="status">正在读取登记和容量…</p>:inventory.data&&(!trees.length?<p>暂无托管工作树。</p>:
        <ul className="settings-data-list">{trees.map(tree=><li key={tree.path}>
          <div><strong>{tree.conversations.join("、")||tree.name}</strong><small>{tree.path}</small></div>
          <span>{tree.available?size(tree.bytes):inventory.data?.reason==="scan_limit"?"本次未计入":"目录不可用"}</span>
        </li>)}</ul>)}
      {inventory.data?.partial&&<p role="status">部分统计。请在存储总览查看详情。</p>}
      <div className="settings-storage-actions"><button onClick={onWorktrees}>返回存储空间</button><button disabled={inventory.isFetching} onClick={()=>void inventory.refetch()}><UiIcon name="reload"/>刷新</button></div>
    </div>:<>
      <section className="settings-storage-summary" data-setting-item="storage" data-highlight={focusItem==="storage"||undefined}>
        <div className="settings-storage-head"><div><small>此设备 · 文件大小</small><strong data-storage-total={inventory.data?.totalBytes}>{inventory.data?size(inventory.data.totalBytes):"—"}</strong></div>
          <button disabled={inventory.isFetching||!!action} onClick={()=>void inventory.refetch()}><UiIcon name="reload"/>{inventory.isFetching?"统计中…":"刷新"}</button></div>
        {inventory.data&&<>
          <div className="settings-storage-bar" role="img" aria-label={inventory.data.categories.map(item=>categories[item.id][0]+" "+size(item.bytes)).join("，")}>
            {inventory.data.categories.filter(item=>item.bytes>0).map(item=><span key={item.id} data-category={item.id} style={{flexGrow:item.bytes}}/>)}
          </div>
          <div className="settings-storage-legend" aria-label="存储分类">{inventory.data.categories.map(item=><button key={item.id} data-category={item.id}
            aria-pressed={selected===item.id} onClick={()=>setSelected(selected===item.id?undefined:item.id)}>
            <span className="settings-storage-dot" aria-hidden="true"/>{categories[item.id][0]}<small data-category-bytes={item.bytes}>{size(item.bytes)}</small>
          </button>)}</div>
          <p className="settings-storage-meta"><span title={inventory.data.dataRoot}>范围 · 应用数据与托管工作树</span><span>上次统计 · {new Date(inventory.data.scannedAt*1000).toLocaleString()}</span></p>
          {inventory.data.partial&&<p role="status">部分统计。{inventory.data.reason==="scan_limit"&&"已到扫描上限。"}{inventory.data.skipped>0&&`跳过 ${inventory.data.skipped} 个链接、已变化或不可读条目。`}</p>}
          {current&&<div className="settings-storage-detail" role="status">
            <strong>{categories[current.id][0]} · {size(current.bytes)} · {current.files.toLocaleString()} 个文件</strong>
            <p>{categories[current.id][1]}</p>
          </div>}
        </>}
      </section>
      <div className="settings-card settings-worktree-link"><div><strong>托管工作树</strong><p>查看位置、关联会话和占用。</p></div><button onClick={onWorktrees}>查看工作树</button></div>
      <section className="settings-card" data-setting-item="archive" data-highlight={focusItem==="archive"||undefined}>
        <h3 ref={archiveHeading} tabIndex={-1}>已归档的会话</h3>
        <p>恢复后可在对话列表继续，原有历史和草稿保留。</p>
        {archived.length>0&&<input type="search" aria-label="搜索归档会话" placeholder="搜索归档会话" value={filter} onChange={e=>setFilter(e.target.value)}/>}
        {conversations.error?<p role="alert">读取归档失败。<button onClick={()=>void conversations.refetch()}>重试</button></p>:
          conversations.isPending?<p role="status">正在读取归档…</p>:!matching.length?<p>{archived.length?"没有匹配的归档会话。":"暂无已归档的会话。"}</p>:
          <ul className="settings-data-list">{matching.map(row=><li key={row.id} data-archived-id={row.id}>
            <div><strong>{row.title||"未命名会话"}</strong><small>{row.projectName||"普通对话"}</small>
              {row.deletionPending&&<small>{t("删除尚未完成")}</small>}
              {row.scheduledPlanId&&<small>{t("定时计划运行记录")}</small>}
            </div>
            <div className="settings-archive-actions">
              {!row.deletionPending&&<button type="button" disabled={!!action||!!row.runtime?.active} onClick={()=>void updateArchive(row,"archive")}>
                {action?.id===row.id&&action.kind==="archive"?t("恢复中…"):t("恢复")}
              </button>}
              {!row.scheduledPlanId&&<button type="button" disabled={!!action||!!row.runtime?.active} onClick={()=>{setDeleteTarget(row);setError("");}}>
                {row.deletionPending?t("重试删除"):t("删除")}
              </button>}
            </div>
          </li>)}</ul>}
        {error&&!deleteTarget&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
      </section>
      {deleteTarget&&<div className="new-project-backdrop" onClick={closeDelete}>
        <Dialog className="new-project-dialog" label={t("永久删除会话")} onClose={closeDelete}>
          <header><h2>{t("永久删除会话")}</h2></header>
          <p>{t("将永久删除「{title}」的聊天记录和导入的附件副本。",{title:deleteTarget.title||t("未命名会话")})}</p>
          {error&&<p role="alert">{error}</p>}
          <footer>
            <button type="button" disabled={!!action} onClick={closeDelete}>{t("取消")}</button>
            <button className="settings-archive-confirm" type="button" disabled={!!action}
              onClick={()=>void updateArchive(deleteTarget,"delete")}>{action?t("删除中…"):t("确认删除")}</button>
          </footer>
        </Dialog>
      </div>}
    </>}
  </>;
}
