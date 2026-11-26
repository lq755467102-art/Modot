// 从变更审查定位到工作间，保留会话、资源、文件和原差异版本。
// 主要引用方包括 `AgentHome.tsx`、`CodeReviewDrawer.tsx` 和 `WorkroomPage.tsx`。
import { createContext, useContext, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, queryString } from "./client";
import { diffLines } from "./codeDiff";
import type { SourceChange, SourceFilePage, SourceRange } from "./generated";
import "./workroom-file-context-menu.css";

type ChangeTarget = {
  conversationId: string; resourceId: string; path: string;
  kind: "operation" | "range"; changeId: string; revision: string; fileIndex: number;
  afterKind: string; line?: number;
};
export type WorkroomFileTarget = {
  conversationId: string; resourceId: string; path: string; revision: string; line?: number;
  scopeRevision?: string;
  agentRequestId?: string;
  gitComparison?: {checkoutPath:string; localHead:string|null; prHead:string; matches:boolean};
  kind?: "conversation-record" | "linked-file-readonly";
};
export type WorkroomFileRequest = WorkroomFileTarget & { serial: number };
type OpenMenu = (event: MouseEvent<HTMLElement>, target: ChangeTarget) => void;
const ChangeMenu = createContext<OpenMenu | undefined>(undefined);
export const useWorkroomChangeMenu = () => useContext(ChangeMenu);

export function workroomLine(target: EventTarget) {
  const line = Number((target as Element).closest<HTMLElement>("[data-new-line]")?.dataset.newLine);
  return Number.isSafeInteger(line) && line > 0 ? line : undefined;
}

// 从审查接口回读实际保留的差异，再用会话、资源与版本锁定工作间要打开的位置。
async function resolveTarget(target: ChangeTarget): Promise<WorkroomFileTarget> {
  const prefix = `/api/v2/conversations/threads/${queryString(target.conversationId)}/${target.kind === "range" ? "change-ranges" : "changes"}/${queryString(target.changeId)}`;
  const source = await api<SourceChange | SourceRange>(`${prefix}?fileOffset=${target.fileIndex}`);
  const file = source.files[target.fileIndex - source.fileOffset];
  if (source.conversationId !== target.conversationId || source.revision !== target.revision ||
      source.resourceId !== target.resourceId || file?.path !== target.path || file.after.kind !== "text" || !file.after.hash)
    throw Error("原改动记录已变化，无法核对工作间位置。");
  let line = target.line;
  if (!line) {
    const diff = await api<SourceFilePage>(`${prefix}/files/${target.fileIndex}?revision=${queryString(target.revision)}`);
    if (diff.path !== target.path || diff.revision !== target.revision)
      throw Error("原差异版本已变化，请在代码审查中核对。");
    const rows = diffLines(diff);
    const changed = rows.findIndex(row => row.kind === "addition" || row.kind === "deletion");
    if (changed !== -1) {
      line = rows.slice(changed).find(row => row.newLine !== undefined)?.newLine ??
        rows.slice(0, changed).reverse().find(row => row.newLine !== undefined)?.newLine;
    }
  }
  return { conversationId: target.conversationId, resourceId: target.resourceId,
    path: target.path, revision: file.after.hash, line };
}

export function WorkroomChangeLinks({ onOpen, active, children }: { onOpen: (target: WorkroomFileTarget) => void; active: string; children: ReactNode }) {
  const [menu, setMenu] = useState<{ target: ChangeTarget; opener: HTMLElement; x: number; y: number }>();
  const current = useRef(menu);
  const view = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function close(restore = false) {
    const opener = current.current?.opener;
    current.current = undefined;
    setMenu(undefined);
    if (restore && opener?.isConnected) opener.focus({ preventScroll: true });
  }
  function openMenu(event: MouseEvent<HTMLElement>, target: ChangeTarget) {
    if (!target.resourceId || !target.changeId || !target.revision) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const opener = (event.target as Element).closest<HTMLElement>("button,[tabindex]") ?? event.currentTarget;
    const next = { target, opener, x: event.clientX || rect.left, y: event.clientY || rect.bottom };
    current.current = next;
    setMenu(next);
    setBusy(false);
    setError("");
  }
  useEffect(() => { close(); }, [active]);
  useEffect(() => {
    if (!menu) return;
    const panel = view.current;
    if (!panel) return;
    panel.style.left = `${Math.max(8, Math.min(menu.x, innerWidth - panel.offsetWidth - 8))}px`;
    panel.style.top = `${Math.max(8, Math.min(menu.y, innerHeight - panel.offsetHeight - 8))}px`;
    (panel.querySelector<HTMLButtonElement>("button:enabled") ?? panel).focus({ preventScroll: true });
    const outside = (event: PointerEvent) => { if (!panel.contains(event.target as Node)) close(); };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close(true);
    };
    const resized = () => close();
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    window.addEventListener("resize", resized);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
      window.removeEventListener("resize", resized);
    };
  }, [menu]);
  async function openFile() {
    const origin = current.current;
    if (!origin || busy) return;
    setBusy(true);
    setError("");
    try {
      const target = await resolveTarget(origin.target);
      // 切会话或重新打开菜单后，旧回执不再触发文件跳转。
      if (current.current === origin) { close(); onOpen(target); }
    } catch (failure) {
      if (current.current === origin) setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (current.current === origin) setBusy(false);
    }
  }
  return <ChangeMenu.Provider value={openMenu}>{children}{menu && createPortal(
    <div className="workroom-file-context-menu" ref={view} aria-label="文件操作" role="group" tabIndex={-1}>
      <button type="button" disabled={busy || menu.target.afterKind !== "text"} onClick={() => void openFile()}>
        {busy ? "正在核对文件位置…" : "在工作间打开"}
      </button>
      {menu.target.afterKind !== "text" && <p>可从代码审查查看此文件的保留版本。</p>}
      {error && <p role="alert">{error}</p>}
    </div>, document.body)}</ChangeMenu.Provider>;
}
