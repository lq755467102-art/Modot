// 呈现普通会话的 Markdown 同步状态，并在工作间分页查看记录。
// 主要引用方包括 `AgentHome.tsx` 和 `WorkroomPage.tsx`。
import type { UiMessage } from "./uiMessages";
import { useUiLanguage } from "./UiLanguage";
import { Fragment, useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { api, APIError, queryString } from "./client";
import type { ConversationRecord, ConversationRecordPage } from "./generated";
import type { WorkroomFileRequest, WorkroomFileTarget } from "./WorkroomChangeLink";
import "./conversation-record.css";

const recordRoute = (id: string) => `/api/v2/conversations/threads/${queryString(id)}/record`;
const readRecord = (id: string) => api<ConversationRecord>(recordRoute(id));

const failureLabels: Partial<Record<string, UiMessage>> = {
  record_directory_unavailable: "会话记录目录暂不可用",
  record_conflict: "会话记录文件有冲突",
  record_history_gap: "会话历史有缺口",
  record_history_unavailable: "会话历史暂不可用",
  record_history_incomplete: "会话历史尚未完整",
  record_write_failed: "会话记录写入失败",
  record_read_failed: "会话记录读取失败",
};

function recordFile(conversationId: string, record?: ConversationRecord): WorkroomFileTarget | undefined {
  if (!record?.resourceId || !record.relativePath || !record.syncedRevision) return;
  return { kind: "conversation-record", conversationId, resourceId: record.resourceId,
    path: record.relativePath, revision: record.syncedRevision };
}

export function ConversationRecordStatus({ conversationId, visible, onOpen }: {
  conversationId: string; visible: boolean; onOpen: (file: WorkroomFileTarget) => void;
}) {
  const { t } = useUiLanguage();
  const query = useQuery({
    queryKey: ["conversation-record", conversationId],
    queryFn: () => readRecord(conversationId),
    enabled: visible,
    refetchInterval: query => query.state.data?.status === "pending" ? 900 : 2500,
  });
  const record = query.data;
  if (!query.error && (!record || record.status === "not_applicable")) return null;
  const file = query.error ? undefined : recordFile(conversationId, record);
  const label = query.error ? "会话记录状态暂不可用"
    : record?.status === "saved" ? "会话记录已保存"
    : record?.status === "pending" ? "正在保存会话记录"
    : record?.status === "blocked" ? failureLabels[record.errorCode ?? ""] ?? "会话记录保存受阻"
    : "会话记录待保存";
  return <div className="composer-record">
    <span role="status" aria-label={t("会话记录保存状态")} data-state={query.error ? "blocked" : record?.status}>
      {t(label)}{!query.error && record?.hasHistoricalTruncation && " · "+t("部分旧正文有截断")}
    </span>
    {file && <button type="button" title={file.path} onClick={() => onOpen(file)}>
      {record?.status === "saved" ? t("在工作间打开记录") : t("打开上次保存的记录")}
    </button>}
  </div>;
}

export function WorkroomConversationRecord({ request, visible, onClose }: {
  request: WorkroomFileRequest; visible: boolean; onClose: () => void;
}) {
  const { t } = useUiLanguage();
  const [source, setSource] = useState<{ file: WorkroomFileTarget; attempt: number }>({ file: request, attempt: 0 });
  const { file, attempt } = source;
  const heading = useRef<HTMLHeadingElement>(null);
  const body = useRef<HTMLPreElement>(null);
  useEffect(() => { if (visible) heading.current?.focus({ preventScroll: true }); }, [visible]);
  const pages = useInfiniteQuery({
    queryKey: ["conversation-record-text", file.conversationId, file.resourceId, file.revision, request.serial, attempt],
    initialPageParam: {revision:file.revision, after:0},
    queryFn: async ({pageParam}) => {
      const read = (revision: string) => api<ConversationRecordPage>(
        `${recordRoute(file.conversationId)}/text?revision=${queryString(revision)}&after=${pageParam.after}`);
      try { return await read(pageParam.revision); }
      catch (error) {
        if (pageParam.after || !(error instanceof APIError) || error.code !== "record_changed") throw error;
        // The first page can race the last save. Once a page is visible, its revision stays pinned.
        const latest = await readRecord(file.conversationId);
        if (latest.resourceId !== file.resourceId || latest.relativePath !== file.path ||
            !latest.syncedRevision || latest.syncedRevision === pageParam.revision) throw error;
        return read(latest.syncedRevision);
      }
    },
    getNextPageParam: (page, _pages, cursor) => page.hasMore && page.nextOffset > cursor.after
      ? {revision:page.revision, after:page.nextOffset} : undefined,
    enabled: visible,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    gcTime: 0,
  });
  const refresh = useMutation({
    mutationFn: async () => {
      const record = await readRecord(request.conversationId);
      const next = recordFile(request.conversationId, record);
      if (!next || next.resourceId !== request.resourceId)
        throw Error(t(failureLabels[record.errorCode ?? ""] ?? "当前会话暂无可打开的记录。"));
      return next;
    },
    onSuccess: file => {
      setSource(old => ({ file, attempt: old.attempt + 1 }));
      body.current?.scrollTo(0, 0);
    },
  });
  const versionChanged = pages.error instanceof APIError && pages.error.code === "record_changed";
  const error = refresh.error?.message ?? (versionChanged ? t("会话记录已更新，请从头读取。") : pages.error?.message);

  return <section className="workroom-record" aria-labelledby="workroom-record-title">
    <header>
      <div>
        <h2 id="workroom-record-title" ref={heading} tabIndex={-1}>{t("会话记录")}</h2>
        <p className="workroom-record-path" title={file.path}>{file.path} · {t("只读")}</p>
      </div>
      <button type="button" onClick={onClose}>{t("返回工作间")}</button>
    </header>
    {pages.isPending && <p role="status">{t("正在读取会话记录…")}</p>}
    {error && <div className="workroom-record-error">
      <span role="alert">{error}</span>
      <button type="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}>{t("重新读取记录")}</button>
    </div>}
    {pages.data && <pre ref={body} className="workroom-record-body" aria-label={t("会话记录正文")} tabIndex={0}>
      {pages.data.pages.map(page => <Fragment key={page.nextOffset}>{page.text}</Fragment>)}
    </pre>}
    {pages.hasNextPage && !error && <footer>
      <button type="button" disabled={pages.isFetchingNextPage} onClick={() => void pages.fetchNextPage()}>
        {pages.isFetchingNextPage ? t("正在读取…") : t("读取更多记录")}
      </button>
    </footer>}
  </section>;
}
