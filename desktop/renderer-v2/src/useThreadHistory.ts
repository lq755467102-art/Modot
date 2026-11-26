// 读取会话事件并按会话保存历史页，区分当前增量与可定位的保留记录。
// 主要引用方包括 `ConversationSideTabs.tsx` 和 `HistoryControls.tsx`。

import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryString } from "./client";
import type { EventRow } from "./domain";

// 后端返回的事件窗。
// `generation` 标识当前日志源，`next_offset` 是同源字节游标，`catchingUp` 表示投影仍在追赶原日志。
export type HistoryPage = {
  events: EventRow[];
  total: number;
  generation: string;
  next_offset: number;
  has_more: boolean;
  gap?: boolean;
  historyNotice?: string;
  catchingUp?: boolean;
};
type HistoryWindow = HistoryPage & {
  sourceChanged?: boolean;
  backlog?: boolean;
  cacheBytes?: number;
};
// Shared by the live tail and the scrolling window. Unchanged event objects cost one encoding.
const byteLengths = new WeakMap<EventRow, number>();
const encoder = new TextEncoder();

export function eventBytes(event: EventRow) {
  const cached = byteLengths.get(event);
  if (cached !== undefined) return cached;
  const bytes = encoder.encode(JSON.stringify(event)).length;
  byteLengths.set(event, bytes);
  return bytes;
}

// 缓存只保留有界的最近窗口；窗口裁剪不等于原始历史被删除，完整旧记录仍沿后端游标读取。
function boundEvents(events: EventRow[]) {
  let bytes = 0;
  const kept: EventRow[] = [];
  for (const event of events.slice(-2000).reverse()) {
    const size = eventBytes(event);
    if (kept.length && bytes + size > 4 * 1024 * 1024) break;
    kept.push(event);
    bytes += size;
  }
  return { events: kept.reverse(), cacheBytes: bytes };
}
// 处理当前事件增量、较旧分页和会话切换；按会话及数据代次保存历史游标。
export function useThreadHistory(
  base: string,
  threadId: string,
  running: boolean,
  visible = true,
) {
  const [pointers, setPointers] = useState(new Map<string, { before: number; generation?: string }>());
  const historyCache = useRef(new Map<string, HistoryWindow>());
  const client = useQueryClient();
  const history = useQuery<HistoryWindow>({
    queryKey: ["history", base, threadId],
    enabled: visible && !!base && !!threadId,
    gcTime: 0,
    refetchIntervalInBackground: true,
    queryFn: async () => {
      const endpoint = base + "/threads/" + queryString(threadId) + "/events";
      let old = historyCache.current.get(threadId);
      let page = await api<HistoryPage>(endpoint + (old && old.next_offset >= 0
        ? "?after_offset=" + old.next_offset + "&limit=500"
        : "?tail=1&limit=500"));
      const retained=historyCache.current.get(threadId);
      if (retained && retained.generation===old?.generation && retained.next_offset===old.next_offset) old=retained;
      // generation 改变说明日志源已重建，先丢弃旧窗口并重新取尾页，不能拼接两个来源的字节游标。
      const changed = !!old && old.generation !== page.generation;
      if (changed) page = await api<HistoryPage>(endpoint + "?tail=1&limit=500");
      if (old && !changed && !page.events.length && old.catchingUp===page.catchingUp &&
          old.total===page.total && old.gap===page.gap && old.historyNotice===page.historyNotice && !old.sourceChanged && !old.backlog)
        return old;
      let events = old && !changed ? [...old.events, ...page.events] : page.events;
      let hasMore = page.has_more;
      // A long streamed role can fill the last page by itself. Keep loading
      // backwards until the latest user turn is present in the main feed.
      if (!old || changed) {
        const run = [...events].reverse().find(e => e.run_id)?.run_id;
        while (run && !events.some(e => e.act === "user" && e.run_id === run)
          && hasMore && events.length < 2000) {
          const first = events[0]?.sourceOffset;
          if (typeof first !== "number" || first <= 0) break;
          const older = await api<HistoryPage>(endpoint + "?tail=1&limit=500&before_offset=" +
            first + "&generation=" + queryString(page.generation));
          if (!older.events.length) break;
          events = [...older.events, ...events];
          hasMore = older.has_more;
        }
      }
      let value: HistoryWindow = {
        ...page,
        events,
        has_more: hasMore,
        sourceChanged: changed,
        backlog: !!old && !changed && page.has_more,
        historyNotice: changed
          ? "日志源已重建，已重新载入当前对话；旧游标不能证明完整性。" +
            (page.historyNotice || "")
          : page.historyNotice,
      };
      value = { ...value, ...boundEvents(value.events) };
      historyCache.current.delete(threadId);
      historyCache.current.set(threadId, value);
      while (
        historyCache.current.size > 16 ||
        (historyCache.current.size > 1 &&
          [...historyCache.current.values()].reduce(
            (sum, item) => sum + (item.cacheBytes || 0),
            0,
          ) >
            8 * 1024 * 1024)
      )
        historyCache.current.delete(historyCache.current.keys().next().value!);
      return value;
    },
    refetchInterval: (query) =>
      !visible ? false : query.state.data?.backlog ? 125 : running || query.state.data?.catchingUp ? document.hidden ? 750 : 250 : 4000,
  });
  const generation = history.data?.generation;
  const pointer = pointers.get(threadId);
  // 轮次定位也绑定来源代次；旧来源的返回位置不再适用于新日志，回到最新窗口重新定位。
  const historyBefore=pointer?.generation===generation ? pointer?.before??0 : 0;
  function setHistoryBefore(before:number) {
    setPointers(previous=>{
      const next=new Map(previous);
      next.delete(threadId);
      if(before>0)next.set(threadId,{before,generation});
      while(next.size>32)next.delete(next.keys().next().value!);
      return next;
    });
  }
  const past = useQuery<HistoryPage>({
    queryKey: ["past", base, threadId, historyBefore, history.data?.generation],
    gcTime: 0,
    queryFn: () =>
      api<HistoryPage>(
        base +
          "/threads/" +
          queryString(threadId) +
          "/events?tail=1&limit=200&before_offset=" +
          historyBefore +
          "&generation=" +
          queryString(history.data!.generation),
      ),
    enabled:
      visible && !!base && !!threadId && !!historyBefore && !!history.data?.generation,
  });
  const visibleHistory = historyBefore ? past.data : history.data;
  function retainTailPrefix(rows:EventRow[], source:string) {
    const live=historyCache.current.get(threadId);
    if(!live || live.generation!==source || !live.events.length)return;
    const seam=rows.findIndex(row=>row.sourceOffset===live.events[0].sourceOffset);
    if(seam<=0)return;
    const next={...live,...boundEvents([...rows.slice(0,seam),...live.events])};
    historyCache.current.set(threadId,next);
    client.setQueryData(["history",base,threadId],next);
  }
  return {
    history,
    past,
    historyBefore,
    setHistoryBefore,
    retainTailPrefix,
    visibleHistory,
  };
}
