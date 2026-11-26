// 为会话页提供复用旧样式的布局容器和插槽，具体行为由会话组件提供。
// 主要引用方包括 `AgentHome.tsx`。
import { useUiLanguage } from "./UiLanguage";
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import originalPage from "../../renderer/index.html?raw";
import { loadJSON, saveJSON } from "./client";
import { wrapDialogTab } from "./useDialogFocus";
import type {ConversationDock} from "./ConversationDock";

type Slots = Record<string, HTMLElement>;
// The original page remains the layout source. Only these existing content
// targets are owned by the managed controller; legacy scripts are not started.
function savedSize(key:string,low:number,high:number,fallback:number){try{const value=Number(localStorage.getItem(key));return value>=low&&value<=high?value:fallback;}catch{return fallback;}}
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 520;
const SIDEBAR_WIDTH_KEY = "legacy-left-width-v2";
function initialSidebarWidth() {
  const legacyWidth = loadJSON("legacy-left-width",
    savedSize("harness.leftw", MIN_SIDEBAR_WIDTH, MAX_SIDEBAR_WIDTH, MIN_SIDEBAR_WIDTH));
  // The previous renderer saved its default on first mount, even without a resize.
  const migrated = legacyWidth === 252 ? MIN_SIDEBAR_WIDTH : legacyWidth;
  return loadJSON(SIDEBAR_WIDTH_KEY, migrated);
}
const targets = { bar: ".bar", sidebar: ".shell > .col:first-child", caption: "#feedcap",
  feed: "#feed", composer: ".composer" };
const template = {__html:new DOMParser().parseFromString(originalPage, "text/html").querySelector(".app")!.outerHTML};

export function useNarrowConversationSidebar() {
  const query = "(max-width: 780px)";
  const [narrow, setNarrow] = useState(() => matchMedia(query).matches);
  useLayoutEffect(() => {
    const media = matchMedia(query);
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return narrow;
}

export function LegacyPage({ slots, feedRef, conversationDock, onFeedMoved, children, theme, fontSize, reviewOpen, review, history, sidebarOpen, sidebarDrawerOpen, onHideSidebar, chatSetup, compact=false }: {
  slots: Record<keyof typeof targets, ReactNode>; feedRef: RefObject<HTMLElement | null>;
  conversationDock: ConversationDock | null;
  onFeedMoved: () => void;
  children?: ReactNode; theme: string; fontSize: number; reviewOpen: boolean; review?:ReactNode;compact?:boolean;
  history?: ReactNode; sidebarOpen: boolean; sidebarDrawerOpen: boolean; onHideSidebar: () => void; chatSetup: boolean;
}) {
  const { t } = useUiLanguage();
  const host = useRef<HTMLDivElement>(null);
  const sidebarDock = useRef<HTMLElement | null>(null);
  const drawerRef = useRef<HTMLDialogElement>(null);
  const drawerComposing = useRef(false);
  const [mounted, setMounted] = useState<Slots>();
  const [left, setLeft] = useState(initialSidebarWidth);
  const [height, setHeight] = useState(() => loadJSON("legacy-composer-height", savedSize("harness.inputh",56,520,86)));
  useLayoutEffect(() => {
    const root = host.current!;
    const found: Slots = {};
    for (const [key, selector] of Object.entries(targets)) {
      const node = root.querySelector<HTMLElement>(selector)!;
      node.replaceChildren(); found[key] = node;
    }
    const dock = found.sidebar;
    sidebarDock.current = dock;
    dock.classList.add("agent-sidebar");
    // Keep the same portal container as the sidebar moves between the dock and drawer.
    const sidebar = document.createElement("div");
    sidebar.id = "conversation-sidebar";
    sidebar.className = "conversation-sidebar-content agent-sidebar";
    dock.append(sidebar);
    found.sidebar = sidebar;
    found.feed.classList.add("agent-messages");
    found.feed.setAttribute("role", "region");
    found.feed.tabIndex = 0;
    found.composer.classList.add("legacy-composer");
    const main = found.feed.parentElement!; main.classList.add("agent-dialogue");
    const reading = document.createElement("div"); reading.className = "dialogue-reading";
    const history = document.createElement("div"); history.className = "dialogue-history-slot";
    main.insertBefore(reading,found.feed); reading.append(history,found.feed); found.history=history;
    root.querySelector("#railside")?.remove();
    root.querySelector("#sbar")?.remove();
    // Preserve the original content slots, with one header beside the sidebar.
    const header=document.createElement('header');header.className='conversation-header';
    main.insertBefore(header,found.caption);header.append(found.caption,found.bar);
    root.querySelector(".shell")!.classList.add("agent-body");
    feedRef.current = found.feed;
    // Browser/review panels are siblings of the original dialogue column.
    const review = document.createElement("div"); review.className = "legacy-review-slot";
    root.querySelector(".shell")!.append(review); found.review = review;
    const extra = document.createElement("div");extra.className="legacy-overlays";root.append(extra);found.extra=extra;
    const splitter = root.querySelector<HTMLElement>("#splitter")!;
    const resize = root.querySelector<HTMLElement>("#cresize")!;
    const listeners: (() => void)[] = [];
    function draggable(el: HTMLElement, dimension: "width" | "height") {
      el.setAttribute("role", "separator");el.tabIndex=0;
      el.setAttribute("aria-orientation",dimension==="width"?"vertical":"horizontal");
      const apply = (value:number) => dimension === "width" ? setLeft(Math.max(MIN_SIDEBAR_WIDTH,Math.min(MAX_SIDEBAR_WIDTH,value))) : setHeight(Math.max(54,Math.min(520,value)));
      const down = (e:PointerEvent) => {
        if(e.button!==0)return;e.preventDefault();el.setPointerCapture(e.pointerId);el.classList.add("dragging");
        const initial=dimension==="width"?e.clientX:e.clientY;
        const size=dimension==="width"?dock.getBoundingClientRect().width:parseFloat(getComputedStyle(root.querySelector('#agent-composer')!).minHeight);
        const move=(m:PointerEvent)=>apply(dimension==="width"?size+m.clientX-initial:size+initial-m.clientY);
        const up=()=>{el.classList.remove("dragging");el.removeEventListener("pointermove",move);el.removeEventListener("pointerup",up);el.removeEventListener("pointercancel",up);};
        el.addEventListener("pointermove",move);el.addEventListener("pointerup",up);el.addEventListener("pointercancel",up);
      };
      const reset=()=>apply(dimension==="width"?MIN_SIDEBAR_WIDTH:86);
      const key=(e:KeyboardEvent)=>{if(["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home"].includes(e.key)){e.preventDefault();if(e.key==="Home")reset();else if(dimension==="width")setLeft(v=>Math.max(MIN_SIDEBAR_WIDTH,Math.min(MAX_SIDEBAR_WIDTH,v+(e.key==="ArrowRight"?12:-12))));else setHeight(v=>Math.max(54,Math.min(520,v+(e.key==="ArrowUp"?12:-12))));}};
      el.addEventListener("pointerdown",down);el.addEventListener("dblclick",reset);el.addEventListener("keydown",key);
      listeners.push(()=>{el.removeEventListener("pointerdown",down);el.removeEventListener("dblclick",reset);el.removeEventListener("keydown",key);});
    }
    draggable(splitter,"width");draggable(resize,"height");
    setMounted(found);
    return()=>{listeners.forEach(f=>f());feedRef.current=null;sidebarDock.current=null;};
  }, [feedRef]);
  const dockFeed = conversationDock?.feed, dockComposer = conversationDock?.composer;
  // 移动原插槽，保留时间线、滚动视口和输入框的挂载。
  useLayoutEffect(()=>{
    if(!mounted||!dockFeed||!dockComposer)return;
    const restore=(['feed','composer'] as const).map(key=>{
      const node=mounted[key], home=document.createComment('main conversation '+key);
      node.before(home);
      (key === "feed" ? dockFeed : dockComposer).append(node);
      return ()=>home.replaceWith(node);
    });
    onFeedMoved();
    return ()=>{
      restore.forEach(moveHome=>moveHome());
      onFeedMoved();
    };
  },[mounted,dockFeed,dockComposer,onFeedMoved]);
  useLayoutEffect(()=>{saveJSON(SIDEBAR_WIDTH_KEY,left);saveJSON("legacy-composer-height",height);},[left,height]);
  useLayoutEffect(()=>{host.current?.querySelector(".shell")?.classList.toggle("has-code-review",reviewOpen);},[reviewOpen,mounted]);
  useLayoutEffect(()=>{
    mounted?.feed.setAttribute("aria-label",t(chatSetup?"新聊天":"对话记录"));
    host.current?.querySelector("#splitter")?.setAttribute("aria-label",t("调整会话侧栏宽度"));
    host.current?.querySelector("#cresize")?.setAttribute("aria-label",t("调整输入框高度"));
  },[mounted,chatSetup,t]);
  useLayoutEffect(() => {
    const drawer = drawerRef.current;
    const dock = sidebarDock.current;
    const panel = mounted?.sidebar;
    if (!drawer || !dock || !panel) return;
    const focused = document.activeElement;
    const previousControl = focused instanceof HTMLElement && panel.contains(focused) ? focused : null;
    const nav = panel.querySelector("nav");
    const scrollTop = nav?.scrollTop;
    if (sidebarDrawerOpen) {
      if (drawer.open) return;
      drawerComposing.current = false;
      drawer.append(panel);
      drawer.showModal();
      if (nav && scrollTop !== undefined) nav.scrollTop = scrollTop;
      const current = panel.querySelector<HTMLButtonElement>('button[aria-current="page"]:not(:disabled)');
      const target = previousControl ?? (current?.getClientRects().length ? current : panel.querySelector<HTMLButtonElement>(".sidebar-drawer-close"));
      target?.focus();
    } else if (drawer.open) {
      drawer.close();
      dock.append(panel);
      drawerComposing.current = false;
      if (!sidebarOpen) {
        host.current?.closest(".application-shell")?.querySelector<HTMLButtonElement>(".conversation-sidebar-toggle")?.focus({ preventScroll: true });
      } else if (previousControl?.getClientRects().length && !panel.closest("[inert]")) {
        if (nav && scrollTop !== undefined) nav.scrollTop = scrollTop;
        previousControl.focus({ preventScroll: true });
      }
    }
  }, [mounted, sidebarDrawerOpen, sidebarOpen]);
  return <div className={`legacy-conversation restored-legacy agent-home theme-${theme}`} data-theme={theme}
    data-sidebar-hidden={!sidebarOpen} data-history-rail={!!history} data-chat-setup={chatSetup} data-density={compact?"compact":"normal"}
    onKeyDown={event => {
      const drawer = drawerRef.current;
      if (drawer?.open && drawer.contains(event.target as Node) && !drawerComposing.current) wrapDialogTab(event.nativeEvent, drawer);
    }}
    onCompositionStartCapture={event => { if (drawerRef.current?.contains(event.target as Node)) drawerComposing.current = true; }}
    onCompositionEndCapture={() => { drawerComposing.current = false; }}
    style={{fontSize,"--fs":fontSize+"px","--leftw":left+"px","--composer-height":height+"px"} as CSSProperties}>
    <div ref={host} className="legacy-original-page" dangerouslySetInnerHTML={template}/>
    <dialog ref={drawerRef} className="conversation-sidebar-drawer" aria-label={t("会话侧栏")}
      onCancel={event => { event.preventDefault(); if (!drawerComposing.current) onHideSidebar(); }}
      onClick={event => {
        if (event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onHideSidebar();
      }}/>
    {mounted && Object.entries(slots).map(([key,content])=>createPortal(content,mounted[key],key))}
    {mounted && createPortal(children,mounted.extra)}
    {mounted && createPortal(review,mounted.review)}
    {mounted && createPortal(history,mounted.history)}
  </div>;
}
