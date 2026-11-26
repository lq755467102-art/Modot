// 展示本轮实际请求的上下文来源、预算、整理记录及下一轮偏好。
// 主要引用方包括 `AgentHome.tsx` 和 `ConversationSettings.tsx`。
import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryString } from "./client";
import { canReadContextResult, ContextResultEvidence } from "./ContextResultEvidence";
import { roleLabel } from "./conversationProjection";
import { InputBudgetDetails } from "./InputBudgetDetails";
import type { ContextCheckpoint, ContextPreferences, ContextSnapshot, ContextSource, ContextView, ResourceSnapshot } from "./generated";
import { valid } from "./validate";
import "./context-inspection.css";

const pageSize = 20;
const preferenceFields = [
  ["facts", "会话事实区头部"],
  ["skills", "可用技能名称及用途"],
] as const;
const resourceKinds = new Map([
  ["code", "代码"],
  ["documents", "资料"],
  ["attachment", "附件"],
]);

// `context_inspection.py` 同时提供请求列表与整理记录，两个偏移各自翻页；快照 ID 为空时由后端选择默认请求。
function readContextView(conversationId: string, snapshotId: string, offset: number, checkpointOffset: number) {
  return api<ContextView>(
    `/api/v2/conversations/threads/${queryString(conversationId)}/context?offset=${offset}&checkpointOffset=${checkpointOffset}` +
      (snapshotId ? "&snapshot=" + queryString(snapshotId) : ""),
  );
}

// 按原请求引用跨页找检查点。若快照版本、引用或页总数变化，结束这次定位，避免混读两个时间点的记录。
async function findRequestCheckpoint(conversationId: string, snapshot: ContextSnapshot, checkpointId: string, signal: AbortSignal) {
  let total: number | undefined;
  for (let offset = 0; ; offset += pageSize) {
    signal.throwIfAborted();
    const page = await readContextView(conversationId, snapshot.id, 0, offset);
    signal.throwIfAborted();
    if (page.snapshot?.id !== snapshot.id || page.snapshot.requestVersion !== snapshot.requestVersion ||
        page.snapshot.checkpointId !== checkpointId) {
      throw new Error("请求快照或检查点引用已变化。");
    }
    total ??= page.totalCheckpoints;
    if (page.totalCheckpoints !== total || (page.checkpoints?.length && page.checkpointOffset !== offset)) {
      throw new Error("整理记录在读取期间发生变化。");
    }
    const match = page.checkpoints?.find(checkpoint => checkpoint.id === checkpointId);
    if (match || !page.checkpointsHasMore) return match ?? null;
    if (page.checkpointOffset !== offset || page.checkpoints?.length !== pageSize ||
        total === undefined || !Number.isSafeInteger(total) || offset + pageSize >= total) {
      throw new Error("无法核对整理记录的下一页。");
    }
  }
}

// 同一后端状态用于请求材料与保留的工具结果，标签按所展示的材料种类解释。
function SourceStatus({ status, result }: { status: string; result: boolean }) {
  if (status === "loaded") return result ? "已保留该页" : "已载入";
  if (status === "not_loaded") return "未载入";
  if (status === "clipped") return "保留节选";
  if (status === "failed") return result ? "保留失败回执" : "加载失败";
  return <>无法核对{status && status !== "unknown" && <> · <code>{status}</code></>}</>;
}

// 仅对可回查的工具结果懒加载 `ContextResultEvidence.tsx`，普通来源行只说明快照保留了什么。
function RequestEntries({ rows, conversationId, result = false }: {
  rows: ContextSource[];
  conversationId: string;
  result?: boolean;
}) {
  const [active, setActive] = useState<number>();
  return <details onToggle={event => {
    if (!event.currentTarget.open) setActive(undefined);
  }}>
    <summary>{result ? "请求中记录的工具结果" : "请求来源"} · {rows.length}</summary>
    {rows.length ? <ul className="context-inspection-entries">
      {rows.map((row, index) => <li key={index}>
        <div className="context-inspection-entry">
          <strong>{row.name}</strong>
          <span><SourceStatus status={row.status} result={result}/></span>
        </div>
        <p>内容版本 <code>{row.version || "未记录"}</code>{row.origin && <> · {row.origin}</>}</p>
        {row.note && <p>{row.note}</p>}
        {result && row.actionId && <p>原动作 <code>{row.actionId}</code></p>}
        {result && canReadContextResult(row) && <>
          <button type="button" aria-expanded={active === index}
            onClick={() => setActive(active === index ? undefined : index)}>
            {active === index ? "收起原回执" : "查看原动作回执"}
          </button>
          {active === index && <ContextResultEvidence conversationId={conversationId} result={row}/>}
        </>}
      </li>)}
    </ul> : <p>{result ? "该快照没有工具结果记录。" : "该快照没有来源记录。"}</p>}
  </details>;
}

function SnapshotConfiguration({ snapshot }: { snapshot: ContextSnapshot }) {
  const { preferences, resourceScope, toolNames } = snapshot;
  const effective = Object.entries(preferences.effective);
  const resources = [resourceScope?.resources, resourceScope?.unavailable]
    .flatMap((rows) => Array.isArray(rows) ? rows : []);

  return <details className="context-inspection-history">
    <summary>历史偏好、资料范围与工具</summary>
    <h4>历史偏好</h4>
    {effective.length ? <ul>
      {effective.map(([name, enabled]) => <li key={name}>
        <strong>{preferenceFields.find(([field]) => field === name)?.[1] ?? name}</strong>
        {" · "}{enabled ? "开启" : "关闭"}
        {preferences.sources[name] && <> · {preferences.sources[name]}</>}
      </li>)}
    </ul> : <p>此快照没有历史偏好记录。</p>}
    {preferences.revision && <p>偏好版本 <code>{preferences.revision}</code></p>}
    <h4>当时的资料范围</h4>
    {resources.length ? <ul>
      {resources.map((row, index) => {
        if (!valid("ResourceSnapshot", row)) return <li key={index}>这项资料的记录无法核对。</li>;
        const resource = row as ResourceSnapshot;
        const readable = resource.permission === "read";
        return <li key={index}>
          <strong>{resource.name}</strong>{" · "}{resourceKinds.get(resource.kind) ?? resource.kind}
          {" · "}{readable ? "当时可读" : "当时无读取权限"}
          {readable && resource.write && " · 可写"}
          {readable && resource.execute && " · 可执行命令"}
        </li>;
      })}
    </ul> : <p>{Array.isArray(resourceScope?.resources) ? "当时没有资料纳入范围。" : "此快照没有资料范围记录。"}</p>}
    <h4>请求提供的工具</h4>
    {toolNames?.length ? <ul className="context-inspection-tool-list">
      {toolNames.map((name, index) => <li key={index}><code>{name || "未记录名称"}</code></li>)}
    </ul> : <p>{toolNames ? "此请求没有提供工具。" : "此快照没有工具清单。"}</p>}
  </details>;
}

// `snapshot` 由后端 `context_inspection.py` 保存，身份与版本绑定此次请求。
// `prepared` 只能证明请求已构建，输出证据通过独立入口核对。
function RequestSnapshot({ snapshot, conversationId }: { snapshot: ContextSnapshot; conversationId: string }) {
  const capacity = snapshot.capacity;
  const [checkpointOpen, setCheckpointOpen] = useState(false);
  const checkpointSummaryId = useId();
  return <article className="context-inspection-snapshot" aria-label="所选请求快照">
    <h3>实际请求 · {roleLabel(snapshot.role)}</h3>
    <p>{snapshot.state === "prepared" ? "已构建请求。" : <>
      请求构建状态无法核对{snapshot.state && <> · <code>{snapshot.state}</code></>}。
    </>}发送和送达暂无可核对记录。</p>
    <p>{snapshot.model.name} · {snapshot.messageCount} 条请求消息</p>
    {capacity?.budget ? <InputBudgetDetails budget={capacity.budget}/> : capacity && <p>
      请求容量{capacity.estimated ? "估算" : "计数"} {capacity.estimatedTotal.toLocaleString()} / {capacity.capacity?.toLocaleString() || "窗口未知"} token
      {" · "}输出预留 {capacity.outputReservationKnown ? capacity.reservedOutput.toLocaleString() : "未知"}
      {" · "}安全余量 {capacity.safetyReserve.toLocaleString()}
    </p>}
    {snapshot.checkpointId && <>
      <p className="context-inspection-checkpoint-link">
        <span>此请求使用的检查点 <code>{snapshot.checkpointId}</code></span>
        <button type="button" aria-expanded={checkpointOpen} aria-controls={checkpointOpen ? checkpointSummaryId : undefined}
          onClick={() => setCheckpointOpen(!checkpointOpen)}>
          {checkpointOpen ? "收起整理记录" : "定位整理记录"}
        </button>
      </p>
      {checkpointOpen && <RequestCheckpoint id={checkpointSummaryId} conversationId={conversationId}
        snapshot={snapshot} checkpointId={snapshot.checkpointId}/>}
    </>}
    <details>
      <summary>快照身份与版本</summary>
      <dl className="context-inspection-identity">
        <dt>快照 ID</dt><dd><code>{snapshot.id}</code></dd>
        <dt>所属轮次</dt><dd><code>{snapshot.runId}</code></dd>
        <dt>请求版本</dt><dd><code>{snapshot.requestVersion}</code></dd>
        <dt>系统消息版本</dt><dd><code>{snapshot.systemVersion}</code></dd>
        <dt>模型端点</dt><dd>{snapshot.model.endpoint} · {snapshot.model.source}</dd>
      </dl>
      {snapshot.notice && <p>{snapshot.notice}</p>}
    </details>
    <RequestEntries rows={snapshot.sources} conversationId={conversationId}/>
    <RequestEntries rows={snapshot.results} conversationId={conversationId} result/>
    {!!snapshot.omittedResults && <p role="status">另有 {snapshot.omittedResults} 页工具结果的元数据超出清单上限。</p>}
    <SnapshotConfiguration snapshot={snapshot}/>
  </article>;
}
// `prefs` 是后端的下一轮偏好。仅保存选中层的草稿，带着原 revision 提交，历史请求快照仍使用原值。
function NextPreferences({
  id,
  prefs,
  onSaved,
  onError,
}: {
  id: string;
  prefs: ContextPreferences;
  onSaved: () => Promise<ContextPreferences>;
  onError: (e: unknown) => void;
}) {
  const [scope, setScope] = useState("conversation");
  const [drafts, setDrafts] = useState<Record<string,Record<string,string>>>({});
  const draft=drafts[scope]||{};
  const setDraft=(value:Record<string,string>)=>setDrafts(old=>({...old,[scope]:value}));
  const [revision,setRevision] = useState(prefs.revision);
  const [busy, setBusy] = useState(false);
  const layer =
    prefs.layers[scope === "global" ? 0 : scope === "project" ? 1 : 2];
  // 把当前层草稿与原修订提交给会话偏好接口，成功后用读回的偏好更新基准。
  async function save() {
    if (
      scope !== "conversation" &&
      !confirm(
        "此变更会影响此范围内的后续运行。偏好不授予任何资源权限。确认保存？",
      )
    )
      return;
    setBusy(true);
    try {
      await api(`/api/v2/conversations/threads/${id}/context`, {
        scope,
        expectedRevision: revision,
        values: Object.fromEntries(
          Object.entries(draft).map(([key, v]) => [
            key,
            v === "inherit" ? null : v === "on",
          ]),
        ),
      });
      const saved = await onSaved();
      setRevision(saved.revision);
      setDraft({});
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <fieldset disabled={busy}>
      <legend>后续运行的系统注入偏好</legend>
      <label>
        设置范围{" "}
        <select
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
          }}
        >
          <option value="conversation">本会话</option>
          <option value="project" disabled={!prefs.layers[1]?.scope}>
            关联项目
          </option>
          <option value="global">全局默认</option>
        </select>
      </label>
      {preferenceFields.map(([name, label]) => (
        <label key={name}>
          {label}{" "}
          <select
            aria-label={label + "偏好"}
            value={
              draft[name] ??
              (layer.values[name] === undefined
                ? "inherit"
                : layer.values[name]
                  ? "on"
                  : "off")
            }
            onChange={(e) => setDraft({ ...draft, [name]: e.target.value })}
          >
            <option value="inherit">继承</option>
            <option value="on">开启</option>
            <option value="off">关闭</option>
          </select>
          当前有效：{prefs.effective[name] ? "开启" : "关闭"}（来自
          {prefs.sources[name]}）
        </label>
      ))}
      <button disabled={!Object.keys(draft).length||prefs.revision!==revision} onClick={save}>
        保存后续偏好
      </button>
      {prefs.revision !== revision && (
        <p role="alert">偏好已被其他入口修改；草稿保留，刷新核对后再保存。</p>
      )}
    </fieldset>
  );
}

// 从会话设置按需加载有效偏好；显式重新核对会用最新值重建编辑草稿。
export function ContextPreferenceSettings({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  const preferences = useQuery<ContextView>({
    queryKey: ["context-preferences", id],
    enabled: open,
    queryFn: () => api("/api/v2/conversations/threads/" + encodeURIComponent(id) + "/context"),
  });
  return <details onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary>上下文偏好</summary>
    {preferences.isFetching&&!preferences.data&&<p role="status">读取偏好中…</p>}
    {(error||preferences.error)&&<p role="alert">{error||preferences.error?.message}</p>}
    {preferences.data&&<NextPreferences key={version} id={id} prefs={preferences.data.preferences}
      onError={cause=>setError(cause instanceof Error?cause.message:String(cause))}
      onSaved={async()=>{
        const result = await preferences.refetch({throwOnError:true});
        setError("");
        return result.data!.preferences;
      }}/>}
    <button onClick={async()=>{
      if (preferences.data&&!confirm("重新载入会丢弃未保存的偏好，继续？")) return;
      const result = await preferences.refetch();
      if (result.isSuccess){setVersion(value=>value+1);setError("");}
    }}>重新核对偏好</button>
  </details>;
}

// 后端增加新的整理策略时仍保留原标识，便于定位尚未识别的记录。
function CheckpointStrategy({ strategy }: { strategy: string }) {
  switch (strategy) {
    case "tool-result-prune": return "裁剪工具结果";
    case "effective-work-checkpoint": return "生成工作检查点";
    case "portable-completed-group-index": return "整理已结束的消息组";
    case "portable-evidence-index": return "整理历史证据索引";
    default: return <>整理方式无法核对{strategy && <> · <code>{strategy}</code></>}</>;
  }
}

// 使用历史检查点记录的用量与保留数；前后任一估算项都会标注估算。
function CheckpointSummary({ checkpoint, usedByRequest }: { checkpoint: ContextCheckpoint; usedByRequest: boolean }) {
  return <>
    <p>{new Date(checkpoint.at * 1000).toLocaleString()} · {roleLabel(checkpoint.role)} · {checkpoint.before.estimatedTotal.toLocaleString()} → {checkpoint.after.estimatedTotal.toLocaleString()} token{(checkpoint.before.estimated || checkpoint.after.estimated) && "（估算）"}</p>
    <p><CheckpointStrategy strategy={checkpoint.strategy}/></p>
    <p>保留 {checkpoint.preservedUserMessages} 条用户原文 · {checkpoint.actionReferences} 个历史动作引用</p>
    <p>检查点 <code>{checkpoint.id}</code> · 轮次 <code>{checkpoint.runId}</code></p>
    {usedByRequest && <p>用于所选请求</p>}
  </>;
}

// 独立定位当前请求实际引用的检查点，定位成功后将焦点移到该摘要。
function RequestCheckpoint({ id, conversationId, snapshot, checkpointId }: {
  id: string; conversationId: string; snapshot: ContextSnapshot; checkpointId: string;
}) {
  const summary = useRef<HTMLElement>(null);
  const lookup = useQuery({
    queryKey: ["context-checkpoint", conversationId, snapshot.id, snapshot.requestVersion, checkpointId],
    queryFn: ({signal}) => findRequestCheckpoint(conversationId, snapshot, checkpointId, signal),
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const checkpoint = lookup.isSuccess && !lookup.isFetching ? lookup.data : undefined;
  useEffect(() => {
    if (checkpoint) summary.current?.focus();
  }, [checkpoint]);

  return <div id={id}>
    {lookup.isFetching && <p role="status">正在定位整理记录…</p>}
    {lookup.isError && !lookup.isFetching && <p role="alert">暂时无法核对整理记录。{lookup.error.message}{" "}
      <button type="button" onClick={() => void lookup.refetch()}>重试定位</button>
    </p>}
    {checkpoint === null && <p role="status">未找到此请求关联的整理记录。</p>}
    {checkpoint && <section className="context-inspection-checkpoint context-inspection-checkpoint-match" tabIndex={-1}
      aria-label="所选请求的整理记录" ref={summary}>
      <CheckpointSummary checkpoint={checkpoint} usedByRequest/>
    </section>}
  </div>;
}

// 保留切页时的列表，但仅展示与当前选择一致且可确认的快照。
export function ContextInspection({
  conversationId,
  forceOpen,
}: {
  forceOpen?: boolean;
  conversationId: string;
}) {
  const [expanded, setOpen] = useState(false);
  const open = forceOpen ?? expanded;
  const [selected, setSelected] = useState("");
  const [offset, setOffset] = useState(0);
  const [checkpointOffset, setCheckpointOffset] = useState(0);
  const view = useQuery<ContextView>({
    queryKey: ["context-inspection", conversationId, selected, offset, checkpointOffset],
    enabled: open,
    // 翻页时保留同一会话的已选快照，换选时等新快照到达再展示。
    placeholderData: (previous, query) =>
      selected && query?.queryKey[1] === conversationId && previous?.snapshot?.id === selected ? previous : undefined,
    retry: false,
    queryFn: () => readContextView(conversationId, selected, offset, checkpointOffset),
  });
  const response = view.error ? undefined : view.data;
  const snapshotMismatch = !!response && !!selected && response.snapshot?.id !== selected;
  const data = snapshotMismatch ? undefined : response;
  const snapshot = data?.snapshot;
  const selectedId = selected || snapshot?.id || "";
  const ready = !!data && !view.isPlaceholderData;
  const listed = data?.snapshots.some(row => row.id === selectedId);
  // 在翻整理记录或请求列表前固定当前请求，否则后端默认最新请求可能切换。
  function pinSnapshot() {
    if (snapshot && !selected) setSelected(snapshot.id);
  }
  // 离开已固定快照时回到最新请求页；已经在默认页时显式重读。
  function loadLatest() {
    if (selected || offset || checkpointOffset) {
      setSelected("");
      setOffset(0);
      setCheckpointOffset(0);
    } else {
      void view.refetch();
    }
  }

  return <section className="context-inspection" aria-label="本轮上下文检查">
    {forceOpen === undefined && <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
      本轮上下文与设置
    </button>}
    {open && <div>
      <div className="context-inspection-controls">
        <label>已保存请求{" "}
          <select aria-label="上下文请求快照" value={selectedId} disabled={!ready || !data.snapshots.length}
            onChange={event => {
              setSelected(event.target.value);
              setCheckpointOffset(0);
            }}>
            {!selectedId && <option value="">请选择请求</option>}
            {selectedId && !listed && <option value={selectedId}>
              {snapshot ? `当前所选 · ${roleLabel(snapshot.role)} · ${snapshot.model.name}` : "当前所选快照"}
            </option>}
            {data?.snapshots.map(row => <option key={row.id} value={row.id}>
              {new Date(row.at * 1000).toLocaleString()} · {roleLabel(row.role)} · {row.model.name}
            </option>)}
          </select>
        </label>
        <button type="button" disabled={!ready || !offset} onClick={() => {
          pinSnapshot();
          setOffset(Math.max(0, offset - pageSize));
        }}>较新请求页</button>
        <button type="button" disabled={!ready || !data.hasMore} onClick={() => {
          pinSnapshot();
          setOffset(offset + pageSize);
        }}>较早请求页</button>
        <button type="button" disabled={view.isFetching} onClick={loadLatest}>载入最新请求</button>
      </div>
      {view.isFetching && !ready && <p role="status">正在读取请求快照…</p>}
      {(view.error || snapshotMismatch) && <p role="alert">
        {snapshotMismatch ? "返回记录与所选请求身份不符。" : <>请求快照暂不可核对。{view.error?.message}</>}{" "}
        <button type="button" disabled={view.isFetching} onClick={() => void view.refetch()}>重新读取所选请求</button>
      </p>}
      {ready && !snapshot && <p role="status">尚无可核对的请求快照。</p>}
      {snapshot && <RequestSnapshot key={JSON.stringify([conversationId, snapshot.id, snapshot.requestVersion, snapshot.checkpointId])}
        snapshot={snapshot} conversationId={conversationId}/>}
      <details>
        <summary>上下文整理记录 · {data?.totalCheckpoints ?? 0}</summary>
        {ready && !data.checkpoints?.length && <p>这一页没有保留的整理记录。</p>}
        {data?.checkpoints?.map(checkpoint => <article className="context-inspection-checkpoint" key={checkpoint.id}>
          <CheckpointSummary checkpoint={checkpoint} usedByRequest={checkpoint.id === snapshot?.checkpointId}/>
        </article>)}
        <div className="context-inspection-controls">
          <button type="button" disabled={!ready || !checkpointOffset} onClick={() => {
            pinSnapshot();
            setCheckpointOffset(Math.max(0, checkpointOffset - pageSize));
          }}>较新整理记录</button>
          <button type="button" disabled={!ready || !data.checkpointsHasMore} onClick={() => {
            pinSnapshot();
            setCheckpointOffset(checkpointOffset + pageSize);
          }}>较早整理记录</button>
        </div>
      </details>
      {ready && <>
        <details>
          <summary>当前有效授权</summary>
          <p>{data.managementNotice}</p>
          <pre>{JSON.stringify(data.authority, null, 2)}</pre>
        </details>
        <details>
          <summary>最近偏好修改记录</summary>
          <pre>{JSON.stringify(data.preferenceChanges, null, 2)}</pre>
        </details>
      </>}
    </div>}
  </section>;
}
