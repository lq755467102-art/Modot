// 管理会话视口的底部跟随与历史阅读锚点，在流式输出和异步布局中保持阅读位置。
// 主要引用方包括 `AgentHome.tsx`。

import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

/** Follow new output only while the reader stays at the bottom. */
export function useConversationScroll(
  element: RefObject<HTMLElement | null>, owner: string, revision: string, paused = false, live = true,
) {
  const positions = useRef(new Map<string, { top: number; following: boolean }>());
  const following = useRef(true);
  const navigating = useRef(false);
  const followedTop = useRef<number | null>(null);
  const viewport = useRef<{ parent: HTMLElement | null; width: number } | null>(null);
  const viewportActions = useRef<{ restore: () => void; rememberPosition: (top: number) => void } | null>(null);
  const pendingRestore = useRef<number | null>(null);
  const ownerChanged=useRef(false);
  const suspended = useRef(paused);
  suspended.current = paused;
  const liveView = useRef(live);
  liveView.current = live;
  const [hasNew, setHasNew] = useState(false);
  // 挂载位置改变时即使视口尺寸相同，浏览器也可能丢弃滚动位置。
  const viewportMoved = useCallback(() => {
    viewport.current = null;
    viewportActions.current?.restore();
  }, []);
  useLayoutEffect(()=>{
    if (!paused) return;
    const node=element.current;
    // 设置打开时视口可能被移回隐藏的对话页，恢复后要重新定位。
    viewport.current = null;
    if (node && node.offsetParent !== null)
      following.current = liveView.current && node.scrollHeight - node.scrollTop - node.clientHeight < 32;
  },[paused,element]);
  function updateViewport(el: HTMLElement) {
    const parent = el.parentElement, width = el.clientWidth;
    if (viewport.current?.parent === parent && viewport.current.width === width) return false;
    viewport.current = { parent, width };
    followedTop.current = null;
    return true;
  }
  // 跟随前先检测用户是否已主动上翻，避免异步渲染把视野拉回底部。
  function followOutput(el:HTMLElement) {
    if (!liveView.current) return;
    // 重新挂载和改变栏宽都可能重置 scrollTop，不能用上个布局的位置判断用户滚动。
    updateViewport(el);
    const prior=followedTop.current;
    // A resize or streaming mutation can run before the next scroll event.
    if (prior !== null && el.scrollTop < prior - 2 && el.scrollHeight - el.clientHeight >= prior - 2) {
      following.current = false;
      viewportActions.current?.rememberPosition(el.scrollTop);
      return;
    }
    el.scrollTop=el.scrollHeight;
    followedTop.current=el.scrollTop;
  }
  useLayoutEffect(() => {
    const el = element.current;
    if (!el) return;
    const position=positions.current.get(owner);
    following.current=position?.following??true;
    navigating.current=false;
    followedTop.current=null;
    pendingRestore.current=position&&!position.following?position.top:null;
    ownerChanged.current=true;setHasNew(false);
    let anchor:{node:HTMLElement; offset:number}|undefined;
    // 保存当前视口第一条有效事件和相对位置，用于内容高度变化后还原。
    function rememberAnchor(){
      const top=el!.getBoundingClientRect().top;
      const node=[...el!.querySelectorAll<HTMLElement>("[data-conversation-item-id]")]
        .find(item=>item.getClientRects().length&&item.getBoundingClientRect().bottom>top+8);
      anchor=node?{node,offset:node.getBoundingClientRect().top-top}:undefined;
    }
    function restoreViewport() {
      if (!el || !updateViewport(el) || el.offsetParent === null) return false;
      if (!following.current && pendingRestore.current === null &&
          anchor && el.contains(anchor.node) && anchor.node.getClientRects().length) {
        const bounds = anchor.node.getBoundingClientRect();
        // 重排后至少让锚点留在可读区域内。
        const offset = Math.max(anchor.offset, 9 - bounds.height);
        el.scrollTop += bounds.top - el.getBoundingClientRect().top - offset;
        rememberAnchor();
      }
      return true;
    }
    function rememberPosition(top: number) {
      if (following.current) followedTop.current = top;
      else rememberAnchor();
      positions.current.delete(owner);
      positions.current.set(owner, { top, following: following.current });
      while (positions.current.size > 32) positions.current.delete(positions.current.keys().next().value!);
    }
    // 在实际可见且历史页挂载完成后记录阅读位置，暂时的加载布局不覆盖原锚点。
    function onScroll() {
      if (suspended.current) return;
      if (restoreViewport()) { follow(); return; }
      if (!el || el.offsetParent === null) return;
      // Async history may temporarily clamp scrollTop to zero. Preserve the
      // old anchor until that page has actually mounted, rather than saving
      // this loading state as the user's new reading position.
      if(pendingRestore.current!==null)return;
      const top = el.scrollTop;
      // A programmatic scroll event can arrive after streamed text has grown again.
      const stillAtFollowedTop = following.current && top === followedTop.current;
      following.current = liveView.current && !navigating.current && (stillAtFollowedTop || el.scrollHeight - top - el.clientHeight < 32);
      rememberPosition(top);
      if (following.current) setHasNew(false);
    }
    // 从渲染变更恢复底部跟随或用户阅读锚点。
    const follow = () => {
      if (suspended.current) return;
      restoreViewport();
      if (el.offsetParent === null) return;
      if(pendingRestore.current!==null){
        const top=pendingRestore.current;
        el.scrollTop=top;
        if(el.scrollHeight-el.clientHeight>=top)pendingRestore.current=null;
        return;
      }
      const selected = window.getSelection();
      const readingSelection = !!selected?.toString() && !!selected.anchorNode && el.contains(selected.anchorNode);
      if (following.current && !readingSelection)
        followOutput(el);
    };
    viewportActions.current = { restore: follow, rememberPosition };
    const layout = new ResizeObserver(follow);
    layout.observe(el);
    const resize = new ResizeObserver(follow);
    // 监听对话行高的异步变化，让渐进渲染与阅读位置协调。
    const watchChildren = () => {
      resize.disconnect();
      for (const child of el.children) resize.observe(child);
      follow();
    };
    const mutation = new MutationObserver(watchChildren);
    // The reading column resize already covers text and expanded details.
    // Reattach only if a direct child is replaced.
    mutation.observe(el, { childList: true });
    watchChildren();
    if(!following.current)rememberAnchor();
    const releaseRestore=()=>{pendingRestore.current=null;navigating.current=false;};
    const onWheel=(event:WheelEvent)=>{
      releaseRestore();
      if(event.deltaY<0){following.current=false;followedTop.current=null;}
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener('wheel',onWheel,{passive:true});el.addEventListener('touchstart',releaseRestore,{passive:true});el.addEventListener('keydown',releaseRestore);
    el.addEventListener('pointerdown',releaseRestore,{passive:true});
    return () => {
      viewportActions.current = null;
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener('wheel', onWheel);
      el.removeEventListener('touchstart', releaseRestore);
      el.removeEventListener('keydown', releaseRestore);
      el.removeEventListener('pointerdown', releaseRestore);
      layout.disconnect();
      resize.disconnect();
      mutation.disconnect();
    };
  }, [owner, element]);
  useLayoutEffect(() => {
    const el = element.current;
    if (!el || el.offsetParent === null || suspended.current) return;
    if(ownerChanged.current){ownerChanged.current=false;return;}
    if(pendingRestore.current!==null)return;
    const selection = window.getSelection();
    const selecting = !!selection?.toString() && !!selection.anchorNode && el.contains(selection.anchorNode);
    if (following.current && !selecting) followOutput(el);
    if (!following.current || selecting) setHasNew(true);
  }, [owner, revision, element]);
  return { hasNew, viewportMoved, jumpTo:(top:number,smooth:boolean)=>{
    pendingRestore.current=null;navigating.current=true;following.current=false;
    element.current?.scrollTo({top,behavior:smooth?"smooth":"instant"});
  // 用户显式返回最新时恢复跟随，刷新当前视口的锚点。
  }, toLatest: () => {
    const el = element.current;
    pendingRestore.current=null;
    navigating.current=false;
    if (el) {el.scrollTop = el.scrollHeight;followedTop.current=el.scrollTop;}
    following.current = true;
    setHasNew(false);
  }};
}
