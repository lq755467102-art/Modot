// 展示会话当前检出目录、运行环境及工作树切换和交接操作。
// 主要引用方包括 `AgentHome.tsx`。
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryString } from "./client";
import type { ConversationScope, ProcessRun, ProjectResource } from "./generated";

// 来自工作间环境接口。
// `path/revision` 用于定位检出目录和并发校验，`setup/actions` 是该目录绑定的准备和运行配置。
type Environment = {path: string; revision: string; setup: string; actions: {name: string; script: string}[]};
type FileStatus = {path: string; status: string};
// 工作树切换预览。
// `source/target` 是实际检出目录，`token` 固定预览，`blocked`、`needSetup` 与 `process` 表示切换前的待处理条件。
type Handoff = {token: string; returning: boolean; source: string; target: string; head?: string;
  blocked: string[]; commits: string[]; needSetup: boolean; setup: string; setupReady: boolean; process?: ProcessRun;
  sourceFiles: FileStatus[]; targetFiles: FileStatus[]};

export type WorktreeChoice = { projectId: string; resourceId: string };

export function hasSelectedWorktree(scope: ConversationScope | undefined, choice: WorktreeChoice) {
  return scope?.projectId === choice.projectId && scope.resources.some(resource =>
    resource.kind === "code" && resource.permission === "read" && resource.worktreeOf === choice.resourceId);
}

export async function prepareConversationWorktree(conversation: string, choice: WorktreeChoice) {
  const base = `/api/v2/conversations/threads/${queryString(conversation)}`;
  let scope = await api<ConversationScope>(base + "/scope");
  if (scope.projectId !== choice.projectId) throw Error("会话归属已变化，请核对当前工作位置。");
  if (hasSelectedWorktree(scope, choice)) return scope;
  if (!scope.resources.some(resource => resource.id === choice.resourceId && resource.kind === "code" && resource.permission === "read")) {
    // 项目默认只选中主目录；在首页选择另一代码目录时，用该目录作为新会话的工作位置。
    const resourceIds = scope.resources.filter(resource => resource.kind !== "code").map(resource => resource.id);
    scope = await api<ConversationScope>(base + "/scope", {
      projectId: choice.projectId, resourceIds: [...resourceIds, choice.resourceId],
    });
  }
  if (hasSelectedWorktree(scope, choice)) return scope;
  const route = base + "/worktree-session";
  const plan = await api<Handoff>(route + "?resource=" + queryString(choice.resourceId));
  if (plan.returning) {
    const current = await api<ConversationScope>(base + "/scope");
    if (hasSelectedWorktree(current, choice)) return current;
    throw Error("工作位置已有新的切换记录，请核对当前工作树。");
  }
  if (plan.blocked.length) throw Error(plan.blocked.join("。"));
  if (plan.needSetup && !plan.setupReady) throw Error("请在“核对工作树”查看并确认初始化脚本。");
  await api<{state: string}>(route, { resourceId: choice.resourceId, token: plan.token, confirm: true });
  const current = await api<ConversationScope>(base + "/scope");
  if (!hasSelectedWorktree(current, choice)) throw Error("工作树切换待确认，请核对当前工作位置。");
  return current;
}

// 按资料原始 ID 切换执行目录，完成后重新读取会话资料范围。
export function ConversationWorkspace({ conversation, kind, resources, onChanged, initialResourceId = "" }: {
  conversation: string; kind: "environment" | "handoff"; resources: ProjectResource[]; onChanged: () => Promise<unknown>;
  initialResourceId?: string;
}) {
  const [selection, setSelection] = useState(initialResourceId);
  const resource = resources.find(r => (r.worktreeOf || r.id) === selection) || (resources.length === 1 ? resources[0] : undefined);
  const id = resource && (resource.worktreeOf || resource.id);
  const route = `/api/v2/conversations/threads/${queryString(conversation)}/${kind === "handoff" ? "worktree-session" : "local-environment"}?resource=${queryString(id || "")}`;
  return <section className="side-content" aria-label={kind === "handoff" ? "切换执行目录" : "本地环境设置"}>
    <header><h2>{kind === "handoff" ? "切换执行目录" : "设置本地环境"}</h2></header>
    {!resources.length ? <p>当前没有已授权的代码工作区。</p> : <label>代码工作区 <select aria-label="代码工作区" value={id || ""} onChange={e => setSelection(e.target.value)}>
      <option value="">请选择</option>
      {resources.map(item => <option key={item.id} value={item.worktreeOf || item.id}>{item.name}{item.worktreeOf ? " · 工作树" : " · 本地"}</option>)}
    </select></label>}
    {id && (kind === "handoff" ? <HandoffView key={route} route={route} resourceId={id} onChanged={onChanged}/>
      : <EnvironmentView key={route + resource.id} route={route} resourceId={id}/>)}
  </section>;
}

function ProcessLog({ run }: {run: ProcessRun}) {
  const output = useQuery({queryKey: ["side-process", run.id], queryFn: () => api<{process: ProcessRun; text: string}>(`/api/v2/processes/${queryString(run.id)}`),
    refetchInterval: q => (q.state.data?.process.state ?? run.state) === "running" ? 800 : false});
  const state = output.data?.process ?? run;
  return <details open><summary>{state.state === "running" ? "正在运行" : state.exitCode === 0 ? "运行完成" : "运行未完成"}</summary>
    {output.error && <p role="alert">{output.error.message}</p>}
    <pre>{output.data?.text || "暂无输出"}</pre>
  </details>;
}

function HandoffView({ route, resourceId, onChanged }: {route: string; resourceId: string; onChanged: () => Promise<unknown>}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const plan = useQuery({queryKey: ["side-handoff", route], queryFn: () => api<Handoff>(route),
    refetchInterval: q => q.state.data?.process?.state === "running" ? 850 : false});
  const view = plan.data;
  async function confirm(skipSetup = false) {
    if (!view || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<{state: string; path?: string}>(route.split("?")[0], {resourceId, token: view.token, confirm: true, skipSetup});
      if (result.state === "switched") {
        setNotice(`已切换至 ${result.path}`);
        await onChanged();
      }
    } catch (cause) {setError((cause as Error).message);}
    finally {await plan.refetch(); setBusy(false);}
  }
  return <>
    {notice && <p role="status">{notice}</p>}
    {(error || plan.error) && <p role="alert">{error || plan.error?.message}</p>}
    <button onClick={() => {setError(""); void plan.refetch();}} disabled={busy}>刷新预览</button>
    {view && !plan.error && <>
      <dl className="side-checkout-plan"><dt>当前目录</dt><dd>{view.source}</dd><dt>目标目录</dt><dd>{view.target}</dd>
        {view.head && <><dt>起点提交</dt><dd>{view.head.slice(0, 12)}</dd></>}
      </dl>
      {!!view.commits.length && <details open><summary>将引入的提交</summary><ul>{view.commits.map(commit => <li key={commit}>{commit}</li>)}</ul></details>}
      <p>忽略文件和本地凭据留在原目录。</p>
      {[["源工作区", view.sourceFiles], ["目标工作区", view.targetFiles]].map(([label, files]) => (files as FileStatus[]).length > 0 &&
        <details key={String(label)} open><summary>{String(label)}的未提交文件</summary><ul>{(files as FileStatus[]).map(file => <li key={file.path}>
          {file.path} · {file.status === "??" ? "未跟踪" : [file.status[0] !== " " && "已暂存", file.status[1] !== " " && "未暂存"].filter(Boolean).join("、")}</li>)}</ul></details>)}
      {!!view.blocked.length && <ul role="status">{view.blocked.map(reason => <li key={reason}>{reason}</li>)}</ul>}
      {view.needSetup && <details open><summary>工作树初始化脚本</summary><pre>{view.setup}</pre></details>}
      {view.process && <ProcessLog key={view.process.id} run={view.process}/>}
      {view.needSetup && !view.setupReady && view.process?.state === "completed" && view.process.exitCode === 0 &&
        <p role="status">脚本已在隔离副本中验证，实际工作树尚未初始化。可在目标目录手动初始化后，选择跳过并切换。</p>}
      <header><button disabled={busy || !!view.blocked.length || plan.isFetching} onClick={() => void confirm()}>
        {view.needSetup && !view.setupReady ? (view.process ? "重试初始化" : "创建工作树并初始化") : view.returning ? "确认切回本地" : "确认移动到工作树"}</button>
        {view.needSetup && !view.setupReady && view.process && view.process.state !== "running" &&
          <button disabled={busy || !!view.blocked.length || plan.isFetching} onClick={() => void confirm(true)}>跳过初始化并切换</button>}
      </header>
    </>}
  </>;
}

function EnvironmentView({ route, resourceId }: {route: string; resourceId: string}) {
  const [draft, setDraft] = useState<Environment>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [run, setRun] = useState<ProcessRun>();
  const config = useQuery({queryKey: ["side-environment", route], queryFn: () => api<Environment>(route)});
  const saved = config.data;
  const view = draft || saved;
  async function change(body: object, revision: string, launch = false) {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (launch) setRun(await api<ProcessRun>(route.split("?")[0], {...body, resourceId, revision, confirm: true}));
      else {
        await api(route.split("?")[0], {...body, resourceId, revision, confirm: true});
        await config.refetch(); setDraft(undefined);
      }
    } catch (cause) {setError((cause as Error).message); await config.refetch();}
    finally {setBusy(false);}
  }
  return <>
    {(error || config.error) && <p role="alert">{error || config.error?.message}</p>}
    {view && saved && <>
      <p className="side-checkout-path">执行目录 {saved.path}</p>
      <label>初始化脚本 <textarea aria-label="初始化脚本" rows={4} value={view.setup} onChange={e => setDraft({...view, setup: e.target.value})}/></label>
      {view.actions.map((action, index) => <fieldset key={index}><legend>常用操作</legend>
        <label>名称 <input value={action.name} onChange={e => setDraft({...view, actions: view.actions.map((a, i) => i === index ? {...a, name: e.target.value} : a)})}/></label>
        <label>命令 <textarea rows={2} value={action.script} onChange={e => setDraft({...view, actions: view.actions.map((a, i) => i === index ? {...a, script: e.target.value} : a)})}/></label>
        <button onClick={() => setDraft({...view, actions: view.actions.filter((_, i) => i !== index)})}>移除</button>
        <button disabled={busy || !!draft} onClick={() => void change({operation: "run", index}, saved.revision, true)}>运行 {action.name}</button>
      </fieldset>)}
      <header><button disabled={busy || view.actions.length >= 5} onClick={() => setDraft({...view, actions: [...view.actions, {name: "", script: ""}]})}>添加操作</button>
        <button disabled={busy || !draft} onClick={() => void change({operation: "save", setup: view.setup, actions: view.actions}, view.revision)}>保存本地环境</button>
        {draft && <button disabled={busy} onClick={() => setDraft(undefined)}>取消编辑</button>}
      </header>
    </>}
    {run && <ProcessLog key={run.id} run={run}/>}
  </>;
}
