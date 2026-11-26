// 在动作证据旁列出该动作涉及的文件，并把所选位置交给统一代码审查视图。
// 主要引用方包括 `ConversationTimeline.tsx` 和 `EvidenceInspector.tsx`。
import { useId, useLayoutEffect, useRef, useState } from "react";
import { AuditStatus } from "./AuditRoundSummary";
import { roleLabel } from "./conversationProjection";
import { evidenceActionState } from "./toolActivityGroups";
import type { ReviewLocation } from "./InlineCodeChanges";
import type { EvidenceAction } from "./generated";

type TargetKind = "read" | "added" | "modified" | "deleted" | "observed" | "unknown" | "attempt" | "conflict";
// 来源是后端 `action_review.py` 的文件关系记录。
// `resourceId/path` 表明文件归属，`kind` 区分实测改动、只读、尝试和未知结果。
export type ActionFileTarget = {
  path: string;
  kind: TargetKind;
  resourceId: string;
  resourceName?: string;
  observedChange?: "added" | "deleted" | "modified";
  changeId?: string;
  revision?: string;
  fileIndex?: number;
};
// `files` 是已核对的证据页，`restricted` 表示读取受限，`conflict` 表示版本冲突，`nextOffset` 供调用者取下一页。
export type ActionReview = {
  files: ActionFileTarget[];
  restricted: boolean;
  reason?: string;
  conflict: boolean;
  nextOffset: number | null;
};
const labels: Record<TargetKind, string> = {
  read: "读取此文件",
  added: "新增此文件",
  modified: "修改此文件",
  deleted: "删除此文件",
  observed: "运行期间观察到改动",
  unknown: "结果待核对",
  attempt: "尝试修改 · 失败",
  conflict: "尝试修改 · 未写入",
};
const observedLabels = {
  added: "运行期间观察到新增", deleted: "运行期间观察到删除", modified: "运行期间观察到修改",
};

// 时间线和证据查看器传入原动作及文件关系。可读定位由调用方打开源码，带变更版本的目标交给统一差异审查。
export function ActionFileReview({action, title, review, conversationId, onDiff, onRead}: {
  action: EvidenceAction;
  title: string;
  review?: ActionReview;
  conversationId: string;
  onDiff: (location: ReviewLocation) => void;
  onRead: (path: string) => void;
}) {
  const graph = useRef<HTMLDivElement>(null);
  const source = useRef<HTMLDivElement>(null);
  const tree = useRef<HTMLDivElement>(null);
  const markerId = useId();
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [lines, setLines] = useState<{ spine: string; branches: { path: string; kind?: string }[] }>();
  const groups = new Map<string, { directory: string; targets: ActionFileTarget[] }>();
  for (const target of review?.files ?? []) {
    const directory = (target.resourceName ? target.resourceName + " / " : "") +
      (target.path.slice(0, target.path.lastIndexOf("/") + 1) || "项目根目录");
    const key = target.resourceId + ":" + directory;
    const group = groups.get(key);
    if (group) group.targets.push(target);
    else groups.set(key, {directory, targets: [target]});
  }
  useLayoutEffect(() => {
    const area = graph.current, card = source.current, folders = tree.current;
    if (!area || !card || !folders) return;
    const draw = () => {
      const box = area.getBoundingClientRect();
      const start = card.getBoundingClientRect();
      const horizontal = folders.getBoundingClientRect().left > start.right + 8;
      const x = horizontal ? start.right - box.left : 10;
      const y = (horizontal ? start.top + start.height / 2 : start.bottom) - box.top;
      const spineX = horizontal ? x + 12 : x;
      const edges = Array.from(folders.querySelectorAll<HTMLElement>("[data-review-edge]")).map(row => {
        const label = row.firstElementChild!.getBoundingClientRect();
        const target = row.lastElementChild!.getBoundingClientRect();
        return {
          x: target.left - box.left - 4,
          y: Math.min(target.bottom - 4, Math.max(target.top + target.height / 2, label.bottom + 3)) - box.top,
          kind: row.dataset.reviewEdge,
        };
      });
      if (!edges.length) { setLines(undefined); return; }
      setLines({
        spine: "M " + x + " " + y + " H " + spineX + " M " + spineX + " " +
          Math.min(y, ...edges.map(edge => edge.y)) + " V " + Math.max(y, ...edges.map(edge => edge.y)),
        branches: edges.map(edge => ({path: "M " + spineX + " " + edge.y + " H " + edge.x, kind: edge.kind})),
      });
    };
    draw();
    const observer = new ResizeObserver(draw);
    for (const element of [area, card, folders]) observer.observe(element);
    return () => observer.disconnect();
  }, [review, folded]);

  return <div className="action-file-review" aria-label="调用与文件关系">
    <div className="action-file-graph" ref={graph} data-has-targets={groups.size > 0}>
      <div className="action-file-source" ref={source}>
        <small>{action.role ? roleLabel(action.role) : "角色未记录"}</small>
        <h3>{title}</h3>
        {review?.conflict ? <span className="evidence-status" data-state="failed">版本冲突 · 未写入</span> : <AuditStatus state={evidenceActionState(action)}/>}
      </div>
      {groups.size > 0 && <div className="action-file-tree" ref={tree} aria-label="涉及的文件">
        {[...groups].map(([key, {directory, targets}]) => {
          const closed = folded.has(key);
          return <section key={key}>
            <div className={closed ? "action-file-edge" : "action-file-folder"}
              data-review-edge={closed ? review?.conflict ? "conflict" : "folder" : undefined}>
              {closed && <small>涉及 {targets.length} 个文件</small>}
              <button type="button" aria-expanded={!closed} title={directory}
                onClick={() => setFolded(old => {
                  const next = new Set(old);
                  if (closed) next.delete(key); else next.add(key);
                  return next;
                })}>
                <span aria-hidden="true">{closed ? "›" : "⌄"} </span>{directory}
              </button>
            </div>
            {!closed && targets.map((target, index) => {
              const name = target.path.split("/").at(-1);
              const {changeId, revision, fileIndex, resourceId} = target;
              const open = changeId && revision && fileIndex !== undefined
                ? () => onDiff({conversationId, changeId, revision, fileIndex, resourceId})
                : target.kind === "read" ? () => onRead(target.path) : undefined;
              return <div className="action-file-edge" key={target.path + ":" + (changeId || index)} data-review-edge={target.kind}>
                <small>{target.observedChange ? observedLabels[target.observedChange] : labels[target.kind]}</small>
                {open ? <button type="button" title={target.path} aria-label={(target.kind === "read" ? "查看读取记录 " : "查看保留差异 ") + target.path}
                  onClick={open}>{name}</button> : <span title={target.path}>{name}</span>}
              </div>;
            })}
          </section>;
        })}
      </div>}
      {groups.size > 0 && lines && <svg aria-hidden="true" className="action-file-lines">
        <defs><marker id={markerId} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 1 1 L 7 4 L 1 7"/>
        </marker></defs>
        <path d={lines.spine} data-kind={review?.conflict ? "conflict" : undefined}/>
        {lines.branches.map((branch, index) => <path key={index} d={branch.path} data-kind={branch.kind} markerEnd={"url(#" + markerId + ")"}/>)}
      </svg>}
    </div>
    {review?.restricted && <p role="status">{review.reason || "这次调用的资料已不在读取范围内。"}</p>}
    {review && !review.restricted && !review.files.length && <p>未记录具体文件。</p>}
  </div>;
}
