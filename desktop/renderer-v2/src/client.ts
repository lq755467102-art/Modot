// 经 Electron IPC 调用工作间后端，核对响应形状，并保留可能尚未确认的操作回执。
// 主要引用方包括 `ApplicationSettings.tsx` 和 `ApplicationIcon.tsx`。

import { validateReply } from "./validate";
import type { LinkedWorkroomFile } from "./generated";
declare global {
  interface Window {
    workbench: {
      installDokan: () => Promise<"ready" | "cancelled" | "restart" | "busy" | "invalid" | "failed" | "newer" | "unavailable">;
      chromeTheme:(theme:string)=>Promise<{theme:string}>;
      interfaceZoom:(percent?:number)=>Promise<import("./InterfaceZoom").InterfaceZoomSetting>;
      closeBehavior:(value?:"background"|"quit")=>Promise<"background"|"quit">;
      edit:(command:"undo"|"redo"|"cut"|"copy"|"paste"|"selectAll")=>Promise<void>;
      reconnect: () => Promise<{ connected: boolean; restarted: boolean }>;
      request: (r: unknown) => Promise<{ status: number; body: any }>;
      displayLanguage: (value?: import("./UiLanguage").LanguageChoice) => Promise<import("./UiLanguage").LanguageSetting>;
      language: (command: Record<string,unknown>) => Promise<unknown>;
      onLanguageChanged: (callback: (state: import('./workroomLanguage').LanguageStatus) => void) => () => void;
      bundledGitHub: () => Promise<{id:string; fingerprint:string; path:string; notice?:string}>;
      chooseProject: (purpose?:'work-root'|'project') => Promise<string | null>;
      chooseEditor: () => Promise<{cancelled?:boolean;name?:string}>;
      openOAuth: (url:string) => Promise<void>;
      management: (command:Record<string,unknown>)=>Promise<any>;
      browser: (command: Record<string, unknown>) => Promise<any>;
      linkedFileAction: (selection: Pick<LinkedWorkroomFile, "conversationId" | "resourceId" | "scopeRevision" | "path" | "absolutePath"> & {
        action:"open" | "choose" | "reveal" | "save"; line?:number;
      }) => Promise<{cancelled?:boolean;path?:string}>;
      openPreview: (selection: {
        conversationId: string;
        previewId: string;
      }) => Promise<{ previewId: string }>;
      openIDE: (w: string, p: string, l: number) => Promise<void>;
      exportOutput: (selection: {
        conversationId: string;
        artifactId?: string;
        versionId?: string;
        attachmentId?: string;
      }) => Promise<{ cancelled?: boolean; path?: string; backup?: string }>;
    };
  }
}
export class APIError extends Error {
  // 保留后端状态、稳定业务码与未确认操作身份，使调用方可读回原回执。
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
const storageKey = "workbench-v2-pending";
// 本地保留的操作索引。
// `id` 用于服务端查原回执，`sendKey` 关联发送草稿，`createdAt` 是本地记录创建的 Unix 毫秒。
type Receipt = { id: string; state: string; path?: string; createdAt?: number; sendKey?: string };
type Reply = Awaited<ReturnType<Window["workbench"]["request"]>>;
const inFlight = new Set<string>();
// 从本地保留数据恢复待确认或可核对的操作，只接纳结构有效的记录。
function receipts(): Record<string, Receipt> {
  try {
    const data = JSON.parse(localStorage.getItem(storageKey) || "{}");
    if (!data || typeof data !== "object" || Array.isArray(data))
      throw Error("invalid receipt storage");
    const result: Record<string, Receipt> = {};
    for (const [key, value] of Object.entries(data)) {
      if (typeof value === "string" && /^[a-zA-Z0-9_-]{8,128}$/.test(value))
        result[key] = { id: value, state: "unknown" };
      else if (
        value &&
        typeof value === "object" &&
        "id" in value &&
        typeof value.id === "string" &&
        /^[a-zA-Z0-9_-]{8,128}$/.test(value.id)
      )
        result[key] = {
          id: value.id,
          state: "state" in value ? String(value.state) : "unknown",
          path: "path" in value ? String(value.path).slice(0, 2048) : undefined,
          createdAt: "createdAt" in value ? Number(value.createdAt) : undefined,
          sendKey: "sendKey" in value && typeof value.sendKey === "string" ? value.sendKey : undefined,
        };
      else throw Error("invalid receipt row");
    }
    return result;
  } catch {
    throw new APIError(
      0,
      "receipt_storage",
      "回执存储无法读取或已损坏，已阻止新写请求。请保留当前资料并核对原回执，不要清空后直接重试。",
    );
  }
}

async function receiptHash(value: string) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), v => v.toString(16).padStart(2, "0")).join("");
}

function outstanding(receipt: Receipt) {
  return receipt.state !== "confirmed" && receipt.state !== "notified";
}

// 经主进程查询原操作编号，`sealMissing` 明确选择是否封口仍找不到的请求。
async function operationStates(ids: string[], sealMissing: boolean): Promise<Record<string, string>> {
  const result = await window.workbench.request({
    path: "/api/operations/reconcile", method: "POST", body: { ids, sealMissing },
  });
  if (result.status !== 200 || !result.body?.states)
    throw new APIError(result.status, "receipt_check", "暂时无法核对原请求，已保留输入。");
  return result.body.states;
}

// 同一草稿先核对原请求：已送达就回取原回执，只有封存为未分派才允许发新请求。
async function recoverSend(receipt: Receipt): Promise<Reply | undefined> {
  if (!inFlight.has(receipt.id)) {
    const states = await operationStates([receipt.id], true);
    if (["completed", "queued", "failed", "not_dispatched"].includes(states[receipt.id])) {
      const result = await window.workbench.request({ path: "/api/operations/" + receipt.id });
      const op = result.body?.operation;
      if (op?.id === receipt.id && op.state === "not_dispatched" && op.response?.code === "request_not_dispatched") return undefined;
      if (result.status === 200 && op?.id === receipt.id && op.response && typeof op.code === "number")
        return { status: op.code, body: op.response };
    }
  }
  throw new APIError(202, "unknown", "原发送结果待确认，已保留草稿。请查看对应会话的进度。");
}

// 统一 POST 操作身份、传输和响应契约校验。送达与结果不明时保留原请求供核对。
export async function api<T = any>(
  path: string,
  body?: unknown,
  independent = false,
  operationId?: string,
  acceptsReceipt?: (body: unknown) => boolean,
): Promise<T> {
  const method = body === undefined ? "GET" : "POST";
  let requestId: string | undefined;
  let key = "";
  let hadReceipt = false;
  let result: Reply | undefined;
  let sendKey: string | undefined;
  // 终端/调试等独立输入不做正文去重复用；普通写请求先持久保存身份，再尝试送达。
  if (method === "POST" && independent) requestId = crypto.randomUUID();
  if (method === "POST" && !independent) {
    // An explicit creation attempt keeps its identity even after a reload or a cleared local receipt.
    const payloadKey = path + "\n" + JSON.stringify(body);
    key = await receiptHash(operationId ? operationId + "\n" + payloadKey : payloadKey);
    const draft = body as { text?: unknown } | null;
    if (/^\/api\/v2\/conversations\/threads\/[^/]+\/send$/.test(path) && typeof draft?.text === "string") {
      // Scope and execution revisions may change while the same draft is
      // waiting for its acknowledgement. Keep its original receipt identity.
      sendKey = await receiptHash(path + "\n" + draft.text);
    }
    let saved = receipts();
    const previous = sendKey && !saved[key]
      ? Object.entries(saved).find(([, receipt]) => receipt.sendKey === sendKey &&
          (!operationId || receipt.state !== "queued"))
      : undefined;
    if (previous) {
      const [previousKey, receipt] = previous;
      result = await recoverSend(receipt);
      saved = receipts();
      if (saved[previousKey]?.id !== receipt.id)
        throw new APIError(202, "unknown", "原发送回执已变化，请核对后重试。");
      if (result) {
        key = previousKey;
        requestId = receipt.id;
      } else {
        delete saved[previousKey];
        localStorage.setItem(storageKey, JSON.stringify(saved));
      }
    }
    if (!result) {
      if (!saved[key] && Object.values(saved).filter(outstanding).length >= 256) {
        await reconcileStoredReceipts(true);
        saved = receipts();
      }
      if (!saved[key] && Object.values(saved).filter(outstanding).length >= 256)
        throw new APIError(0, "receipt_capacity", "仍有256项回执待核对，暂时无法提交新请求。");
      hadReceipt = !!saved[key];
      requestId = saved[key]?.id || operationId || crypto.randomUUID();
      saved[key] = {
        ...saved[key], id: requestId, state: "sending", path, sendKey,
        createdAt: saved[key]?.createdAt || Date.now(),
      };
      try {
        localStorage.setItem(storageKey, JSON.stringify(saved));
      } catch {
        throw new APIError(0, "receipt_storage", "无法持久保存请求回执，尚未发送。请释放存储空间并保留输入。");
      }
    }
  }
  if (!result) {
    if (requestId) inFlight.add(requestId);
    try {
      result = await window.workbench.request({ path, method, body, requestId });
    // 网络失败不能证明写操作未发生；保留 unknown 回执，让再次提交核对原操作而非盲目重发。
    } catch {
      if (key) {
        const saved = receipts();
        if (saved[key]?.id === requestId) {
          saved[key].state = "unknown";
          try { localStorage.setItem(storageKey, JSON.stringify(saved)); } catch {}
        }
      }
      throw new APIError(
        0,
        "unknown",
        independent
          ? "交互输入结果未确认。请核对终端或调试状态；系统不会自动重放。"
          : method === "POST"
            ? "请求结果未确认。输入已保留；再次提交相同内容会核对原回执。"
            : "本地服务连接中断，已有内容已保留",
      );
    } finally {
      if (requestId) inFlight.delete(requestId);
    }
  }
  if (!result.body || typeof result.body !== "object")
    throw new APIError(502, "protocol", "接口格式不兼容");
  // 有独立运行记录的路由可以确认领取回执，后续状态仍从那条记录读取。
  const accepted = result.status === 202 && acceptsReceipt?.(result.body) === true;
  if (result.status === 200 && !validateReply(path, method, result.body))
    throw new APIError(
      502,
      "protocol",
      "工程接口返回与共享协议不一致；请更新前后端后重试",
    );
  if (key) {
    const saved = receipts();
    if (saved[key]?.id === requestId) {
      // queued/202/版本不兼容都不等于任务完成；已有回执的失败回复也要保留待核对状态。
      if (result.body.queued || result.body.resume?.queued)
        saved[key].state = "queued";
      else if ((result.status === 202 && !accepted) || result.body.code === "client_version" || result.body.code === "data_version" ||
          (hadReceipt && result.status >= 400 && result.body.code !== "request_not_dispatched"))
        saved[key].state = "unknown";
      else delete saved[key];
      try {
        localStorage.setItem(storageKey, JSON.stringify(saved));
      } catch {
        window.dispatchEvent(new Event("workbench-storage-error"));
      }
    }
  }
  if (result.status >= 400 || (result.status === 202 && !accepted) || result.body.ok === false)
    throw new APIError(
      result.status,
      result.body.code || "unknown",
      result.body.error || result.body.why || result.body.note || "操作未完成",
    );
  const queuedSend = result.body.queued && path.match(/^\/api\/v2\/conversations\/threads\/([^/]+)\/send$/);
  if (queuedSend && requestId) trackFollowup(decodeURIComponent(queuedSend[1]), requestId, true);
  return result.body as T;
}
// The renderer keeps receipt IDs only. Text, dispatch and cancellation come from the original operation.
export function followupIds(owner: string): string[] {
  const ids = loadJSON<unknown>("followup-receipts:" + owner, []);
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}
export function trackFollowup(owner: string, id: string, retain: boolean) {
  const ids = followupIds(owner);
  if (ids.includes(id) === retain) return;
  saveJSON("followup-receipts:" + owner, retain ? [...ids, id] : ids.filter(x => x !== id));
  window.dispatchEvent(new CustomEvent("modot-followup-receipts", {detail: owner}));
}
export const workbenchBootstrapPath = "/api/v2/bootstrap?includeGit=0";
export const wsPath = (id: string, action = "") =>
  "/api/v2/workspaces/" + encodeURIComponent(id) + (action ? "/" + action : "");
export const queryString = (value: string) => encodeURIComponent(value);
let reconciling = false;
// 批量核对已登记且已离开当前发送栈的请求，根据后端事实更新本地回执。
async function reconcileStoredReceipts(sealMissing: boolean): Promise<string[]> {
  const pending = Object.entries(receipts())
    .filter(([, row]) => outstanding(row) && !inFlight.has(row.id))
    .slice(0, 256);
  if (!pending.length) return [];
  const states = await operationStates(pending.map(([, row]) => row.id), sealMissing);
  const saved = receipts();
  const notes: string[] = [];
  let changed = false;
  for (const [key, original] of pending) {
    const row = saved[key];
    // 核对期间可能有新发送或回执替换，只能更新仍对应原请求且不在传输中的那一项。
    if (row?.id !== original.id || !outstanding(row) || inFlight.has(row.id)) continue;
    const state = states[row.id];
    if (state === "failed" || state === "not_dispatched") {
      if (state === "failed") notes.push("原请求失败，请查看对应会话：" + (row.path || row.id));
      delete saved[key];
    } else if (state === "completed") {
      if (row.state === "queued") delete saved[key];
      else {
        row.state = "confirmed";
        notes.push("原请求已确认收讫，请查看对应会话的实际进度：" + (row.path || row.id));
      }
    } else if (state === "unknown" && !sealMissing) {
      row.state = "notified";
      notes.push("请求结果未知，系统不会自动重放：" + (row.path || row.id) + "（回执 " + row.id + "）");
    } else continue;
    changed = true;
  }
  if (changed) localStorage.setItem(storageKey, JSON.stringify(saved));
  return notes;
}

// 应用重载后只核对原请求是否确已处理，拒绝并发重复核对。
export async function reconcileReceipts(): Promise<string[]> {
  if (reconciling) return [];
  reconciling = true;
  try {
    return await reconcileStoredReceipts(false);
  } catch {
    return [];
  } finally {
    reconciling = false;
  }
}
let storageWarning = false;
// 为渲染器保存本地界面值，写入失败只报告一次以免反复打扰。
export function saveJSON(key: string, value: unknown): void;
export function saveJSON(key: string, value: unknown, confirm: true): boolean;
export function saveJSON(key: string, value: unknown, confirm?: true) {
  try {
    localStorage.setItem("workbench-v2-" + key, JSON.stringify(value));
    if (confirm) return true;
  } catch {
    if (!storageWarning) {
      storageWarning = true;
      window.dispatchEvent(new Event("workbench-storage-error"));
    }
    if (confirm) return false;
  }
}
// 旧偏好缺失或不可读取时回到调用方提供的默认值。
export function loadJSON<T>(key: string, fallback: T): T {
  try {
    return (
      JSON.parse(localStorage.getItem("workbench-v2-" + key) || "null") ||
      fallback
    );
  } catch {
    return fallback;
  }
}
