// 维护会话历史的有界阅读窗口，按滚动方向加载记录并保留阅读锚点。
// 主要引用方包括 `AgentHome.tsx` 和 `ConversationSideTabs.tsx`。
import { useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { api, queryString } from "./client";
import type { EventRow } from "./domain";
import { eventBytes, useThreadHistory, type HistoryPage } from "./useThreadHistory";

type Direction = "older" | "newer";
type Slice = { page: HistoryPage; older: boolean; newer: boolean };
type Anchor = { owner: string; rows: {id:string; offset?:string; top:number}[] };
const messagesPerPage = 120;

function readingRows(row: EventRow) {
  if (row.act === "user") return 1;
  if (row.act !== "say" || row.toolPreludeFallback === true) return 0;
  // A published progress message can also split out a visible process header.
  return row.toolPrelude ? 2 : 1;
}

// A reply may span many streaming events. Bound both the event data and independent messages.
function bounded(rows: EventRow[], keepStart: boolean) {
  let bytes = 0, messages = 0, count = 0;
  for (let i = 0; i < rows.length && i < 2000; i++) {
    const row = rows[keepStart ? i : rows.length - 1 - i];
    const n = eventBytes(row);
    if (count && (bytes + n > 4 * 1024 * 1024 || messages >= 360)) break;
    bytes += n;
    count++;
    messages += readingRows(row);
  }
  if (count === rows.length) return rows;
  return keepStart ? rows.slice(0, count) : rows.slice(-count);
}

export function useThreadReader(
  base: string, id: string, running: boolean, feed: RefObject<HTMLElement | null>, visible = true,
) {
  const state = useThreadHistory(base, id, running, visible);
  const seed = state.visibleHistory;
  const tail = state.history.data;
  const liveWindow = useMemo(() => tail && { ...tail, events: bounded(tail.events, false) }, [tail]);
  const initial = useMemo(() => {
    if (seed === tail) return liveWindow;
    return seed && { ...seed, events: bounded(seed.events, false) };
  }, [seed, tail, liveWindow]);

  const owner = JSON.stringify([base, id, tail?.generation, state.historyBefore]);
  const [windows, setWindows] = useState(() => new Map<string, Slice>());
  const [failure, setFailure] = useState<{ owner: string; direction: Direction; message: string }>();
  const active = useRef<symbol | null>(null);
  const readingDirection = useRef<Direction | undefined>(undefined);
  const recheckEdge = useRef<(() => void) | null>(null);
  const anchor = useRef<Anchor | null>(null);
  const current = useRef(owner);
  current.current = owner;

  function setSlice(slice: Slice | undefined) {
    setWindows(previous => {
      const next = new Map(previous);
      next.delete(owner);
      if (slice) next.set(owner, slice);
      // The live tail has its own cache. Only the two most recent reading windows are retained.
      while (next.size > 2) next.delete(next.keys().next().value!);
      return next;
    });
  }

  const read = windows.get(owner);
  const visibleHistory = read?.page ?? initial;
  const first = visibleHistory?.events[0]?.sourceOffset;
  const last = visibleHistory?.events.at(-1)?.sourceOffset;
  const liveLast = tail?.events.at(-1)?.sourceOffset;
  const canReadOlder = read?.older ?? (typeof first === "number" && first > 0);
  const canReadNewer = read
    ? read.newer || (liveLast !== undefined && last !== undefined && liveLast > last)
    : !!state.historyBefore;

  useLayoutEffect(() => {
    active.current = null;
    readingDirection.current = undefined;
    if (anchor.current?.owner !== id) anchor.current = null;
    setFailure(undefined);
  }, [owner, id]);

  // 追加旧页或调整可见窗口前，记录视区中的稳定行和位置，供布局完成后恢复。
  function saveAnchor(node: HTMLElement) {
    const viewport = node.getBoundingClientRect();
    const rows: Anchor["rows"] = [];
    for (const item of node.querySelectorAll<HTMLElement>("[data-conversation-item-id]")) {
      if (!item.checkVisibility()) continue;
      const bounds = item.getBoundingClientRect();
      if (bounds.top >= viewport.bottom) break;
      if (bounds.bottom <= viewport.top + 2) continue;
      rows.push({id:item.dataset.conversationItemId!,offset:item.dataset.historyOffset,top:bounds.top-viewport.top});
      if (rows.length===4) break;
    }
    anchor.current = rows.length ? {owner:id,rows} : null;
  }

  useLayoutEffect(() => {
    const node = feed.current, saved = anchor.current;
    if (!node || saved?.owner !== id || !visibleHistory) return;
    anchor.current = null;
    for (const item of saved.rows) {
      let row = node.querySelector<HTMLElement>('[data-conversation-item-id="' + CSS.escape(item.id) + '"]');
      if (!row?.checkVisibility() && item.offset) {
        row = node.querySelector<HTMLElement>('[data-history-offset="' + CSS.escape(item.offset) + '"]');
      }
      if (!row?.checkVisibility()) continue;
      node.scrollTop += row.getBoundingClientRect().top - node.getBoundingClientRect().top - item.top;
      break;
    }
  }, [visibleHistory, owner, feed]);

  async function load(direction: Direction) {
    const older = direction === "older";
    const cursor = older ? first : last;
    const node = feed.current;
    if (!node || !visibleHistory || cursor === undefined || active.current) return;
    if (!(older ? canReadOlder : canReadNewer)) return;
    const ticket = Symbol();
    active.current = ticket;
    setFailure(undefined);
    try {
      // A page full of streaming deltas can represent less than one visible
      // round. Use the existing API limit to avoid repeated reads at that edge.
      const rowsInView = visibleHistory.events;
      const messages = rowsInView.reduce((total, row) => total + readingRows(row), 0);
      const size = Math.min(500,Math.max(messagesPerPage,Math.ceil(messagesPerPage*rowsInView.length/Math.max(1,messages))));
      const params = new URLSearchParams({ generation: visibleHistory.generation, limit: String(size) });
      if (older) {
        params.set("tail", "1");
        params.set("before_offset", String(cursor));
      } else {
        params.set("after_offset", String(cursor));
      }
      const page = await api<HistoryPage>(base + "/threads/" + queryString(id) + "/events?" + params);
      if (current.current !== owner || active.current !== ticket) return;
      if (readingDirection.current && readingDirection.current!==direction) {
        recheckEdge.current?.();
        return;
      }
      if (page.generation !== visibleHistory.generation) {
        void state.history.refetch();
        return;
      }

      const added = page.events.filter(e => typeof e.sourceOffset === "number" &&
        (older ? e.sourceOffset < cursor : e.sourceOffset > cursor));
      const rows = older ? [...added, ...visibleHistory.events] : [...visibleHistory.events, ...added];
      saveAnchor(node);
      let events = bounded(rows, older);
      const pinned = Number(anchor.current?.rows[0]?.offset);
      if (Number.isFinite(pinned) && events.length < rows.length &&
          (older ? events.at(-1)!.sourceOffset! < pinned : events[0].sourceOffset! > pinned)) {
        // A small viewport can span hundreds of collapsed events. Keep its
        // leading visible row and take only as much of the new page as fits.
        const at = rows.findIndex(row=>row.sourceOffset!>=pinned);
        if (at>=0) {
          const anchored = older ? bounded(rows.slice(0,at+1),false) : bounded(rows.slice(Math.max(0,at-1)),true);
          if (older ? anchored[0].sourceOffset! < cursor : anchored.at(-1)!.sourceOffset! > cursor) events=anchored;
        }
      }
      const clippedStart=events[0]!==rows[0], clippedEnd=events.at(-1)!==rows.at(-1);
      // Resume polling the live tail only when the end of this page actually overlaps it.
      if (!older && !clippedEnd && !page.has_more && tail?.events.some(e => e.sourceOffset === events.at(-1)?.sourceOffset)) {
        state.retainTailPrefix(events,visibleHistory.generation);
        setSlice(undefined);
        if (state.historyBefore) state.setHistoryBefore(0);
      } else {
        setSlice({
          page: { ...page, events },
          older: clippedStart || (older ? page.has_more : canReadOlder),
          newer: clippedEnd || (older ? canReadNewer : page.has_more),
        });
      }
    } catch (error) {
      if (current.current === owner && active.current === ticket)
        setFailure({ owner, direction, message: (error as Error).message });
    } finally {
      if (active.current === ticket) active.current = null;
    }
  }

  const handler = useRef(load);
  handler.current = load;
  useLayoutEffect(() => {
    const node = feed.current;
    if (!node || !visible || !id) return;
    let previous = node.scrollTop;
    let frame = 0, dragging = false;
    const edge = () => {
      frame = 0;
      if (readingDirection.current === "older" && node.scrollTop < Math.min(180, node.clientHeight / 2)) {
        void handler.current("older");
      } else if (readingDirection.current === "newer" && node.scrollHeight - node.scrollTop - node.clientHeight < 180) {
        void handler.current("newer");
      }
    };
    const check = () => { if (!frame) frame = requestAnimationFrame(edge); };
    recheckEdge.current = check;
    const wheel = (event: WheelEvent) => {
      if (!event.deltaY) return;
      readingDirection.current = event.deltaY < 0 ? "older" : "newer";
      check();
    };
    const key = (event: KeyboardEvent) => {
      if (["ArrowUp", "PageUp", "Home"].includes(event.key)) readingDirection.current = "older";
      else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) readingDirection.current = "newer";
      else return;
      check();
    };
    const down = () => { dragging = true; readingDirection.current = undefined; };
    const up = () => { dragging = false; };
    const onScroll = () => {
      if (dragging && node.scrollTop !== previous) readingDirection.current = node.scrollTop < previous ? "older" : "newer";
      previous = node.scrollTop;
      check();
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    node.addEventListener("wheel", wheel, { passive: true });
    node.addEventListener("keydown", key);
    node.addEventListener("pointerdown", down);
    window.addEventListener("pointerup", up);
    node.addEventListener("touchstart", down, { passive: true });
    node.addEventListener("touchend", up, { passive: true });
    return () => {
      recheckEdge.current = null;
      cancelAnimationFrame(frame);
      node.removeEventListener("scroll", onScroll);
      node.removeEventListener("wheel", wheel);
      node.removeEventListener("keydown", key);
      node.removeEventListener("pointerdown", down);
      window.removeEventListener("pointerup", up);
      node.removeEventListener("touchstart", down);
      node.removeEventListener("touchend", up);
    };
  }, [feed, id, visible, owner]);

  function setHistoryBefore(value: number) {
    active.current = null;
    anchor.current = null;
    setSlice(undefined);
    setFailure(undefined);
    state.setHistoryBefore(value);
  }
  return {
    ...state, liveWindow, visibleHistory,
    historyBefore: state.historyBefore || (read ? -1 : 0),
    setHistoryBefore,
    readFailure: failure?.owner === owner ? failure : undefined,
    retryRead: () => { if (failure) void load(failure.direction); },
  };
}
