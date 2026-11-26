// 展示项目范围内的关联会话运行，并提供原轮次跳转入口。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";
import { RetainedDialog } from "./RetainedDialog";

// `runId` 与 `conversationId` 一起定位来源，列表标题只用于展示。
// `started` 和 `finished` 是 Unix 秒。
type Run = {
  runId: string; conversationId: string; title: string; state: string;
  started: number; finished: number | null; task: string | null; result?: string | null;
};
// 在途和历史运行由后端分别分页，各自游标只用于对应列表。
type Activity = {
  active: Run[]; nextActive: number | null; history: Run[]; nextBefore: number | null;
};

const states: Record<string, string> = {
  running: "进行中", completed: "已完成", failed: "未完成", interrupted: "已暂停",
  waiting_approval: "等待确认", unknown: "结果待核对",
};
const time = (seconds: number) => new Date(seconds * 1000).toLocaleString();

// 按当前会话的项目范围查询活动；点击记录把原会话及运行交回调用方定位。
export function ProjectActivity({ conversationId, onSelect }: {
  conversationId: string;
  onSelect: (id: string, run: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pages, setPages] = useState<Activity[]>([]);
  const [loading, setLoading] = useState(false);
  const [pageError, setPageError] = useState("");
  const [activePages, setActivePages] = useState<number[]>([]);
  const activeBefore = activePages.at(-1);
  const path = "/api/v2/conversations/threads/" + encodeURIComponent(conversationId) + "/project-activity";
  const query = new URLSearchParams({ view: "progress" });
  if (activeBefore) query.set("activeBefore", String(activeBefore));
  const activity = useQuery({
    queryKey: [path, "history", activeBefore], queryFn: () => api<Activity>(path + "?" + query),
    enabled: open, refetchInterval: open ? 3000 : false, retry: false,
  });
  const archive = activity.error ? undefined : activity.data;
  // 重新打开时从最新页展开，避免复用上次的分页位置。
  function openActivity() {
    setPages([]);setPageError("");
    setActivePages([]);
    setOpen(true);
  }
  // 跳转的是运行来源会话，可能与发起查看的会话不同。
  function source(run: Run) {
    setOpen(false);
    onSelect(run.conversationId,run.runId);
  }
  const next = (pages.at(-1) ?? archive)?.nextBefore;
  // 沿历史游标追加下一页，保留已有页供列表去重。
  async function loadOlder() {
    if(!next||loading)return;
    setLoading(true);setPageError("");
    try{const older=await api<Activity>(path+"?view=progress&before="+next);setPages(old=>old.length?[...old,older]:archive?[archive,older]:[older]);}
    catch(e){setPageError((e as Error).message);}
    finally{setLoading(false);}
  }
  const description = (run:Run) => run.result || (run.task ? "任务："+run.task : "查看原轮次");
  function item(run: Run, showTitle = true) {
    return <button type="button" className="project-activity-entry" onClick={()=>source(run)}>
      {showTitle&&<strong>{run.title}</strong>}<time>{time(run.finished ?? run.started)}</time>
      <span className="project-activity-task">{description(run)}</span>
      {run.state!=="completed"&&<small>{states[run.state] ?? "结果待核对"}</small>}
    </button>;
  }
  const groups: Run[][] = [];
  const seen = new Set<string>();
  for(const run of [...archive?.history ?? [],...pages.flatMap(page=>page.history)]){
    if(seen.has(run.runId))continue;
    seen.add(run.runId);
    const group=groups.at(-1),latest=group?.[0];
    if(group&&latest?.conversationId===run.conversationId&&latest.state==="completed"&&run.state==="completed"&&
      new Date((latest.finished??latest.started)*1000).toDateString()===new Date((run.finished??run.started)*1000).toDateString())group.push(run);
    else groups.push([run]);
  }
  return <>
    <button type="button" className="project-activity-trigger" onClick={openActivity}>
      项目动态
    </button>
    <RetainedDialog open={open} label="项目动态" onClose={() => setOpen(false)}>
      <div className="project-activity">
        {activity.isPending && <p role="status">正在读取会话进度</p>}
        {activity.error && <p role="alert">{String(activity.error)}</p>}
        {archive && <>
          <section aria-label="进行中"><h3>进行中</h3>{archive.active.length?<ul className="project-activity-list">{archive.active.map(run=><li key={run.runId}>{item(run)}</li>)}</ul>:<p>暂无记录</p>}
            {activePages.length > 0 && <button type="button" onClick={() => setActivePages(activePages.slice(0, -1))}>较新的进行中任务</button>}
            {archive.nextActive !== null && <button type="button" onClick={() => setActivePages([...activePages, archive.nextActive!])}>更早的进行中任务</button>}
          </section>
          <section aria-label="任务留档"><h3>任务留档</h3>{groups.length?<ul className="project-activity-list">{groups.map(([run,...rest])=><li key={run.runId}>
            {rest.length?<details className="project-activity-group"><summary><strong>{run.title}</strong><time>{time(rest.at(-1)!.finished??rest.at(-1)!.started)} – {time(run.finished??run.started)}</time><span className="project-activity-task">{description(run)}</span><small>{rest.length+1} 条</small></summary>
              <ol>{[run,...rest].map(entry=><li key={entry.runId}>{item(entry,false)}</li>)}</ol></details>:item(run)}
          </li>)}</ul>:<p>暂无记录</p>}
            {pageError&&<p role="alert">{pageError}</p>}
            {!!next&&<button type="button" disabled={loading} onClick={loadOlder}>{loading?"正在读取…":"更早任务"}</button>}
          </section>
        </>}
      </div>
    </RetainedDialog>
  </>;
}
