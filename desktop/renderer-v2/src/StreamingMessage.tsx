// 把正在生成的消息逐步显现为可读正文，收尾时使用同一保留消息内容。
// 主要引用方包括 `ConversationTimeline.tsx`。

import {useEffect,useLayoutEffect,useMemo,useRef,useState} from "react";
import {Message} from "./Panels";
import {useApplicationSettings} from "./ApplicationSettings";
import {draftMarkdownPrefix,planDraftMarkdown} from "./streamingMarkdown";
import {proseFadeDuration} from "./draftFade";

const graphemes = new Intl.Segmenter();

// Original display preference: reveal received text smoothly, or paint each
// received block directly. Neither option hides a live response until final.
export function StreamingMessage({text,streaming,reveal=false,reasoning=false,immediate=false,settle=false,visibleUntil,onEvidence,onRevealed}:{text:string;streaming:boolean;reveal?:boolean;reasoning?:boolean;immediate?:boolean;settle?:boolean;visibleUntil?:number;onEvidence?:(id:string)=>void;onRevealed?:()=>void}){
  const {display,reducedMotion}=useApplicationSettings();
  const typeOutput=!reducedMotion&&(reasoning||display.output==='type');
  const wasStreaming=useRef(streaming);
  if(streaming)wasStreaming.current=true;
  const [through,setThrough]=useState(()=>!immediate&&(streaming||reveal)&&typeOutput?0:text.length);
  const cursor=useRef(through);
  const lastAdvance=useRef(-Infinity);
  const typed=useRef(false);
  // A final say event can arrive before the previous chunk has finished
  // revealing. Keep the same message animating through its final text.
  const animate=!immediate&&typeOutput&&(streaming||reveal||(wasStreaming.current&&through<text.length));
  if(animate)typed.current=true;
  const clipped = visibleUntil !== undefined && visibleUntil < text.length;
  const complete = !clipped && (!animate || through >= text.length);
  const reportsReveal = !!onRevealed;
  const pending = streaming || !complete || (animate && reportsReveal);
  const markdown = useMemo(() => pending ? planDraftMarkdown(text, streaming) : undefined, [pending, streaming, text]);
  const available = Math.min(markdown?.available ?? text.length, visibleUntil ?? text.length);
  const revealCallback = useRef(onRevealed);
  useLayoutEffect(() => { revealCallback.current = onRevealed; }, [onRevealed]);
  useEffect(() => {
    // Wait for a closed source and the last glyph fade. Incoming events in
    // neighbouring rows can refresh the callback while the fade is running.
    if (!complete || streaming || !reportsReveal) return;
    const notify = revealCallback.current;
    if (!animate) { notify?.(); return; }
    const fade = window.setTimeout(() => notify?.(), proseFadeDuration + 5);
    return () => window.clearTimeout(fade);
  }, [complete, animate, streaming, reportsReveal, text]);

  useEffect(()=>{
    if(!animate||complete||cursor.current>=available)return;
    const from=cursor.current;
    // A new chunk can extend the last displayed grapheme, for example with
    // a combining mark. Re-segment from the start of that cluster.
    const start=graphemes.segment(text).containing(from)?.index??from;
    const ends=Array.from(graphemes.segment(text.slice(start,available)),
      part=>start+part.index+part.segment.length);
    // Usually advance one visible character at a time. An unusually large
    // burst or final correction gets a bounded number of paint steps.
    const step=Math.max(1,Math.ceil(ends.length/(settle?32:48)));
    const interval=step===1&&ends.length<28?24:15;
    let index=0;
    function advance() {
      lastAdvance.current=performance.now();
      index=Math.min(ends.length,index+step);
      cursor.current=ends[index-1];
      setThrough(cursor.current);
      if(index===ends.length)window.clearInterval(timer);
    }
    // Electron may throttle animation frames in an occluded window. Keep
    // the reveal cursor current independently of the visual CSS transition.
    const timer=window.setInterval(advance,interval);
    if(performance.now()-lastAdvance.current>=interval)advance();
    return()=>window.clearInterval(timer);
  },[animate,available,complete,settle,text]);
  useEffect(()=>{
    if(!animate){
      cursor.current=available;
      setThrough(available);
    }
  },[animate,available]);
  const fade=typeOutput&&typed.current&&(streaming||animate);
  const draft=markdown?draftMarkdownPrefix(text,animate?Math.min(through,available):available,markdown):{text,suffix:""};
  return <Message text={draft.text} markdownSuffix={draft.suffix} streaming={pending} fade={fade} onEvidence={onEvidence}/>;
}
