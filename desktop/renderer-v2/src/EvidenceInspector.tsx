// 沿会话、轮次和动作身份查看证据、输出与关联文件。
// 主要引用方包括 `AgentHome.tsx`。
import {UiIcon} from "./UiIcon";
import {AuditRoundSummary, AuditStatus} from "./AuditRoundSummary";
import {toolTitle} from "./toolActivityGroups";
import {roleLabel} from "./conversationProjection";
import { CodeBlock } from "./CodeBlock";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { APIError, api, queryString } from "./client";
import { checkOperationCandidate, findCheckEvidence, matchesCheckEvidence, readRecordedCheckLog, type EvidenceGap } from "./checkEvidence";
import { GapEvidence } from "./GapEvidence";
import { captureDialogReturn } from "./useDialogFocus";
import { CorrectionForm } from "./Corrections";
import { RestorationControl } from "./RestorationControl";
import type { RestorationPlan } from "./generated";
import type { EvidenceAction, EvidencePage, EvidenceOutput } from "./generated";

import { ActionFileReview, type ActionReview } from "./ActionFileReview";
import type { ReviewLocation } from "./InlineCodeChanges";

type ActionDetail = EvidenceAction & {displayTitle?: string; review: ActionReview};
type ListedAction = EvidenceAction & {targetPath?: string; displayTitle?: string};

export function EvidenceInspector({
  conversationId,
  selected,
  initialRun = "",
  initialGap,
  onSelect,
  onClose,
  onQuestion,
  allowCorrections = false,
  onRestore,
  onReview,
}: {
  conversationId: string;
  selected: string | null;
  initialRun?: string;
  initialGap?: EvidenceGap;
  onSelect: (id: string | null) => void;
  onClose: () => void;
  onQuestion?: (question: { id: string; text: string }) => void | Promise<void>;
  allowCorrections?: boolean;
  onReview: (location: ReviewLocation) => void;
  onRestore?: (plan: RestorationPlan, source: EvidenceAction) => Promise<void>;
}) {
  const [screen,setScreen] = useState<"gap"|"check"|"single"|"summary"|"steps">(initialGap?"gap":selected?"single":"summary");
  // Entering from the progress view keeps that opener even when an action is selected later.
  const [focusOnOpen] = useState(() => !!(initialGap || (initialRun && !selected)));
  const [summaryRun,setSummaryRun] = useState(initialRun);
  const inspector = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const stepScroll = useRef(0);
  const singleScroll = useRef(0);
  const [panel,setPanel] = useState<"output"|"question"|"correction">("output");
  const [recordOpen, setRecordOpen] = useState(false);
  const [readPath, setReadPath] = useState("");
  const [fileOffset, setFileOffset] = useState(0);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [questionError, setQuestionError] = useState("");
  const [failed, setFailed] = useState(false),
    [role, setRole] = useState(""),
    [tool, setTool] = useState(""),
    [run, setRun] = useState(initialRun);
  const [before, setBefore] = useState<number | null>(null),
    [offset, setOffset] = useState(0);
  const [checkPage, setCheckPage] = useState<{revision:string; offset:number}>();
  const [checkNotice, setCheckNotice] = useState("");
  const checking = screen === "check";
  const initialCheckOperation = initialGap?.kind === "check" ? initialGap.id : "";
  useEffect(() => {
    if (!focusOnOpen) return;
    let restore: (()=>void) | undefined;
    const frame = requestAnimationFrame(() => {
      restore = captureDialogReturn();
      heading.current?.focus({preventScroll:true});
    });
    return () => {cancelAnimationFrame(frame);restore?.();};
  }, [focusOnOpen]);
  useEffect(() => {
    setOffset(0);
    setFileOffset(0);
    setReadPath("");
    setRecordOpen(false);
    setQuestion("");
    setQuestionError("");
    setPanel("output");
    singleScroll.current = 0;
    if (selected) setScreen("single");
  }, [selected]);
  const base = "/api/v2/conversations/threads/" + conversationId + "/evidence";
  const params = new URLSearchParams({
    failed: failed ? "1" : "0",
    role,
    tool,
    run,
    order: "asc",
  });
  if (before) params.set("after", String(before));
  const list = useQuery<EvidencePage>({
    queryKey: ["evidence", conversationId, failed, role, tool, run, before],
    queryFn: () => api(base + "?" + params),
    enabled:screen==="steps",
    refetchInterval: before ? false : 3000,
  });
  const checkTarget = useQuery({
    queryKey: ["check-evidence-target", conversationId, initialRun, initialCheckOperation],
    queryFn: ({signal}) => findCheckEvidence(conversationId, initialRun, initialCheckOperation, signal),
    enabled: checking,
    retry: false,
  });
  const actionId = checking ? checkTarget.data?.actionId : selected;
  const detail = useQuery<ActionDetail>({
    queryKey: [checking ? "check-evidence-detail" : "evidence-detail", conversationId, actionId, fileOffset],
    enabled: !!actionId,
    queryFn: () => api<ActionDetail>(base + "/" + queryString(actionId!) + "?fileOffset=" + fileOffset),
    refetchInterval: 3000,
    refetchIntervalInBackground: true,
    ...(checking ? {retry:false} : {}),
  });
  const d = detail.error ? undefined : detail.data;
  const canRead = d?.review?.files.some(file => file.kind === "read" && file.path === readPath);
  const checkRevision = checking && d && actionId && d.review?.restricted === false &&
    matchesCheckEvidence(d, conversationId, initialRun, initialCheckOperation, actionId) ? d.evidenceRevision : undefined;
  const readOffset = checking ? (checkPage?.revision === checkRevision ? checkPage?.offset ?? 0 : 0) : offset;
  const live = !!checkRevision && d?.state === "running";
  const output = useQuery<EvidenceOutput>({
    queryKey: [checking ? "check-evidence-output" : "evidence-output", conversationId, actionId, d?.evidenceRevision, readOffset, live],
    enabled: checking ? !!checkRevision : !!d && !d.review.restricted && screen === "single" && (recordOpen || !!canRead),
    queryFn: ({signal}) => checking && !live ? readRecordedCheckLog({
      conversationId, runId: initialRun, operationId: initialCheckOperation,
      target: checkTarget.data!, evidenceRevision: checkRevision!, after: readOffset, signal,
    }) : api(base + "/" + queryString(actionId!) + "/output?format=readable&after=" + readOffset + (live ? "&live=1" : "")),
    refetchInterval: live && readOffset === 0 ? 1500 : false,
    ...(checking ? {retry:false} : {}),
  });
  const outputRevision = output.data?.evidenceRevision;
  const outputError = output.error instanceof APIError ? output.error : undefined;
  const versionChanged = !!checkRevision && (outputError?.code === "evidence_changed" || (!!outputRevision && outputRevision !== checkRevision));
  const refreshDetail = detail.refetch;
  useEffect(() => {
    if (!versionChanged) return;
    let current = true;
    // A check may finish between reading its identity and reading its log.
    void refreshDetail().then(result => {
      if (!current || result.error || result.data?.evidenceRevision !== checkRevision) return;
      setCheckNotice("原检查的日志版本暂时无法核对。");
      setScreen("summary");
    });
    return () => {current = false;};
  }, [versionChanged, checkRevision, outputRevision, refreshDetail]);
  let checkFailure = "";
  if (checking) {
    if (checkTarget.error || (checkTarget.isSuccess && !checkTarget.data)) {
      checkFailure = "暂时无法将这一项定位到本轮的检查操作。";
    } else if (detail.error || (d && !checkRevision)) {
      checkFailure = d?.review?.restricted ? "原检查的日志已不在当前读取范围内。" : "原检查的动作与轮次暂时无法核对。";
    } else if (output.error && !versionChanged) {
      checkFailure = outputError?.status === 403 ? "原检查的日志已不在当前读取范围内。"
        : outputError?.code === "wrong_source" ? outputError.message : "原检查的日志未保留或暂不可读取。";
    }
  }
  useEffect(() => {
    if (!checkFailure) return;
    setCheckNotice(checkFailure);
    setScreen("summary");
  }, [checkFailure]);

  function showOutputPage(next: number) {
    if (checking && checkRevision) setCheckPage({revision:checkRevision, offset:next});
    else setOffset(next);
  }
  useEffect(() => {
    if(screen==="steps"){
      if(inspector.current)inspector.current.scrollTop=stepScroll.current;
    } else if (inspector.current) {
      inspector.current.scrollTop = screen === "single" ? singleScroll.current : 0;
    }
  }, [screen,selected]);
  function choose(id: string) {
    if (screen === "steps") stepScroll.current = inspector.current?.scrollTop || 0;
    if (selected !== id) {
      singleScroll.current = 0;
      setOffset(0);
      onSelect(id);
    }
    setScreen("single");
  }
  const outputBody = <>
    {output.error && <p role="alert">{output.error.message}</p>}
    {output.isPending && <p role="status">{checking ? "正在读取检查日志…" : "正在读取保留的工具回执…"}</p>}
    {output.data && !output.error && <>
      {output.data.text && <CodeBlock text={output.data.text} label={checking ? "检查日志" : canRead ? "读取记录" : "工具输出"}/>}
      {!output.data.complete && <p role="status">{output.data.reason}</p>}
      {output.data.hasMore && <button disabled={output.isFetching} onClick={() => showOutputPage(output.data!.next)}>继续查看</button>}
      {readOffset > 0 && <button onClick={() => showOutputPage(0)}>回到输出开头</button>}
    </>}
  </>;
  const actionDetail = d && screen === "single" ? (
        <section key={d.id} className="evidence-detail-card" aria-label="工具动作详情" data-action-id={d.id}>
          <ActionFileReview action={d} title={d.displayTitle || toolTitle(d.tool)} review={d.review}
            conversationId={conversationId} onDiff={onReview}
            onRead={path => { setPanel("output"); setReadPath(path); setOffset(0); }}/>
          {!d.review.restricted && !!(d.impact?.imageView || d.impact?.imageArtifact) &&
            <ConversationImageCard conversation={conversationId} action={d.id} version={d.evidenceRevision} title="图片证据" state={d.state}/>}
          {!!fileOffset && <button onClick={()=>setFileOffset(n=>Math.max(0,n-100))}>上一页文件</button>}
          {d.review.nextOffset != null && <button onClick={()=>setFileOffset(d.review.nextOffset!)}>后续文件</button>}
          {canRead && panel==="output" && <section className="evidence-card-body" aria-label="保留的读取记录">
            <h4>{readPath}</h4>{outputBody}
          </section>}
          {onRestore &&
            typeof d.impact?.operationId === "string" &&
            typeof d.impact?.backupId === "string" && (
              <RestorationControl
                key={"restore:" + d.id}
                conversationId={conversationId}
                operationId={d.impact.operationId}
                onDelegate={(plan) => onRestore(plan, d)}
              />
            )}
          {!d.review.restricted && <nav className="evidence-card-tools" aria-label="这一步的操作">
            {onQuestion&&<button aria-pressed={panel==="question"} onClick={()=>setPanel(panel==="question"?"output":"question")}><UiIcon name="compose"/>问 Agent</button>}
            {allowCorrections&&d.evidenceRevision&&<button aria-pressed={panel==="correction"} onClick={()=>setPanel(panel==="correction"?"output":"correction")}><UiIcon name="reload"/>指出问题</button>}
          </nav>}
          {!d.review.restricted && panel==="correction" && allowCorrections && d.evidenceRevision && (
            <div className="evidence-card-body">
            <CorrectionForm
              key={d.id}
              conversationId={conversationId}
              target={{
                kind: "action",
                id: d.id,
                revision: d.evidenceRevision,
              }}
            />
            </div>
          )}
          {!d.review.restricted && panel==="question" && onQuestion && (
            <fieldset className="evidence-card-body">
              <legend>让 Agent 解释这一步</legend>
              <label>
                想了解什么
                <textarea
                  aria-label="证据追问内容"
                  value={question}
                  maxLength={4000}
                  onChange={(e) => setQuestion(e.target.value)}
                />
              </label>
              <button
                disabled={asking || !question.trim()}
                onClick={async () => {
                  setAsking(true);
                  setQuestionError("");
                  try {
                    const child = await api<{ id: string }>(
                      "/api/v2/conversations/threads/" +
                        conversationId +
                        "/questions",
                      { actionId: d.id },
                    );
                    await onQuestion({ id: child.id, text: question });
                  } catch (e) {
                    setQuestionError(
                      e instanceof Error ? e.message : String(e),
                    );
                  } finally {
                    setAsking(false);
                  }
                }}
              >
                在对话中继续
              </button>
              <p>
                将在独立对话中带上这一步的记录。问题会放入输入框，由你发送。
              </p>
              {questionError && <p role="alert">{questionError}</p>}
            </fieldset>
          )}
          {!d.review.restricted && <details className="evidence-card-body" onToggle={e=>setRecordOpen(e.currentTarget.open)}>
          <summary>查看调用记录</summary>
          {recordOpen && <>
          <p>工具 <code>{d.tool||"未知"}</code> · 状态 <code>{d.state}</code></p>
          <code>{d.id}</code>
          <dl className="evidence-record-grid">
            <dt>角色 / 轮次</dt>
            <dd>
              {d.role || "未记录"} / {d.runId || "未记录"}
            </dd>
            <dt>执行进程位置</dt>
            <dd>{d.location || "未记录"}</dd>
            <dt>开始 / 结束</dt>
            <dd>
              {d.startedAt
                ? new Date(d.startedAt * 1000).toLocaleString()
                : "未记录"}{" "}
              /{" "}
              {d.finishedAt
                ? new Date(d.finishedAt * 1000).toLocaleString()
                : "未记录"}
            </dd>
          </dl>
          {!!d.missingFields.length && (
            <p>缺失关联：{d.missingFields.join("、")}。未知字段保留未知。</p>
          )}
          {d.identityKind === "legacy-record" && (
            <p>此编号仅定位历史记录，不代表恢复了原动作身份。</p>
          )}
          <h4>输入（已脱敏，超限会标注）</h4>
          <pre>{JSON.stringify(d.input ?? "未记录", null, 2)}</pre>
          <h4>工具报告的资源影响</h4>
          <pre>{JSON.stringify(d.impact || {}, null, 2)}</pre>
          <h4>审批引用</h4>
          <p>
            {d.approvalRefs?.length
              ? d.approvalRefs.join("、")
              : "没有记录到审批关联，不能据此认定已审批"}
          </p>
          {d.error && !output.data?.text.includes(d.error) && <p className="evidence-result-note" data-state="failed">{d.error}</p>}
          {canRead && panel==="output" ? <p>工具输出显示在上方的读取记录。</p> : outputBody}
          </>}
          </details>}
        </section>
       ) : null;
  return (
    <aside ref={inspector} aria-label="工具证据审查" className="evidence-inspector">
      <header>
        <h2 ref={heading} tabIndex={focusOnOpen ? -1 : undefined}>{screen === "gap" ? "原始证据" : checking ? "检查日志" : screen === "single" ? "单次调用审查" : "工具证据审查"}</h2>
        <button onClick={onClose}>关闭证据审查</button>
      </header>
      <nav className="audit-view-switch" aria-label="证据审查视图">
        {(screen === "gap" || checking) ? <button type="button" onClick={()=>setScreen("summary")}>查看本轮证据</button> : screen === "single" ? <button onClick={() => {
          singleScroll.current = inspector.current?.scrollTop || 0;
          const ownerRun = d?.runId || initialRun;
          setSummaryRun(ownerRun);
          setRun(ownerRun);
          setBefore(null);
          setRole(""); setTool(""); setFailed(false);
          setScreen("steps");
        }}>查看本轮调用</button> : <>
          {selected && <button onClick={() => {
            if (screen === "steps") stepScroll.current = inspector.current?.scrollTop || 0;
            setScreen("single");
          }}>返回单次调用</button>}
          <button aria-pressed={screen==="summary"} onClick={() => {
            if (screen==="steps") stepScroll.current = inspector.current?.scrollTop || 0;
            setScreen("summary");
          }}>本轮摘要</button>
          <button aria-pressed={screen==="steps"} onClick={()=>setScreen("steps")}>全部步骤</button>
        </>}
      </nav>
      {screen === "gap" && initialGap && <GapEvidence conversationId={conversationId} runId={initialRun} gap={initialGap}
        onUnavailable={(notice, endpointMissing) => {
          if (endpointMissing && checkOperationCandidate(initialGap)) {
            setScreen("check");
          } else {
            setCheckNotice(notice);
            setScreen("summary");
          }
        }}/>}
      {checking && !checkFailure && (checkRevision && !versionChanged ? <section className="evidence-card-body" aria-label="原检查日志">
        {checkTarget.data?.revision && <p>检查时的源码版本 <code>{checkTarget.data.revision}</code></p>}
        {live && output.data && typeof output.data.bytes !== "number"
          ? <p role="status">检查仍在运行，日志暂未可读。</p> : outputBody}
        {live && output.data?.bytes === 0 && <p role="status">该检查暂未产生文本输出。</p>}
      </section> : <p role="status">正在定位原检查日志…</p>)}
      {screen === "single" && selected && <>
        {detail.isPending && <p role="status">正在核对这次调用…</p>}
        {detail.error && <p role="alert">{detail.error.message} <button onClick={()=>detail.refetch()}>重试</button></p>}
        {actionDetail}
      </>}
      {screen==="summary"&&checkNotice&&<p role="status">{checkNotice} 已打开本轮证据。</p>}
      {screen==="summary"&&<AuditRoundSummary base={base} run={summaryRun}
        onRun={value=>{setSummaryRun(value);setRun(value);setBefore(null);}}
        onEvidence={(id,ownerRun)=>{setRun(ownerRun);setBefore(null);choose(id);}}/>}
      <div hidden={screen!=="steps"}>
      <details><summary>筛选动作</summary>
      <fieldset>
        <legend>筛选实际动作</legend>
        <label>
          <input
            type="checkbox"
            checked={failed}
            onChange={(e) => {
              setFailed(e.target.checked);
              setBefore(null);
            }}
          />
          仅失败
        </label>
        <label>
          角色
          <select
            value={role}
            onChange={(e) => {
              setRole(e.target.value);
              setBefore(null);
            }}
          >
            <option value="">全部角色（含未知）</option>
            <option value="executor">执行者</option>
            <option value="planner">规划者</option>
            <option value="reviewer">检查者</option>
          </select>
        </label>
        <label>
          工具
          <input
            value={tool}
            onChange={(e) => {
              setTool(e.target.value);
              setBefore(null);
            }}
            maxLength={128}
          />
        </label>
      </fieldset>
      </details>
      {list.error && <p role="alert">{list.error.message}</p>}
      {list.data && !list.error && (
        <>
          {(list.data.gapCount > 0 ||
            list.data.malformedCount > 0 ||
            list.data.sequenceConflicts > 0) && (
            <p role="status">
              原始记录存在缺口、损坏或冲突，当前证据可能不完整。
            </p>
          )}
          {list.data.catchingUp && (
            <p role="status">正在分批补齐历史索引，稍后更新。</p>
          )}
          {!list.data.actions.length && (
            <p>没有匹配动作。</p>
          )}
          <ul className="evidence-actions">
            {(list.data.actions as ListedAction[]).map(action => <li key={action.id}>
              <button className="activity-row evidence-heading" data-evidence-action={action.id}
                onClick={()=>choose(action.id)} aria-current={selected===action.id?"true":undefined}>
                <strong>{action.displayTitle || toolTitle(action.tool)}{action.targetPath ? " · " + action.targetPath : ""}</strong>
                <AuditStatus state={action.state}/>
                <small>{action.role ? roleLabel(action.role) : "角色未知"}
                  {action.startedAt ? " · " + new Date(action.startedAt * 1000).toLocaleTimeString() : ""}</small>
              </button>
            </li>)}
          </ul>
          {list.data.hasMore && (
            <button onClick={() => setBefore(list.data!.nextBefore!)}>
              后续调用
            </button>
          )}
          {before && (
            <button onClick={() => setBefore(null)}>回到本轮开头</button>
          )}
        </>
      )}
      </div>
    </aside>
  );
}
import {ConversationImageCard} from "./ConversationImageCard";
