import {useLayoutEffect,useRef,useState,type ReactNode} from "react";
import {useApplicationSettings} from "./ApplicationSettings";

/** Keep the native disclosure open just long enough to paint its exit. */
export function StepDisclosure({open,onOpen,summary,children,className,live,foldOnMount=false}:{
  open:boolean;onOpen:(open:boolean)=>void;summary:ReactNode;children:(present:boolean)=>ReactNode;
  className:string;live?:boolean;foldOnMount?:boolean;
}) {
  const {reducedMotion}=useApplicationSettings();
  const [retained,setRetained]=useState(open||foldOnMount&&!reducedMotion);
  const root=useRef<HTMLDetailsElement>(null);
  const previous=useRef(retained);
  const motion=useRef<Animation[]>([]);
  const motionTimeout=useRef<number|undefined>(undefined);
  function cancelMotion() {
    window.clearTimeout(motionTimeout.current);
    motionTimeout.current=undefined;
    for(const animation of motion.current)animation.cancel();
    motion.current=[];
  }
  const present=open||retained;
  useLayoutEffect(()=>{
    const el=root.current;
    if(!el||previous.current===open&&!motion.current.length)return;
    previous.current=open;
    const label=el.firstElementChild as HTMLElement;
    const content=label.nextElementSibling as HTMLElement;
    const reversing=motion.current.length>0;
    const closed=label.offsetHeight;
    const from=open&&!reversing?closed:el.getBoundingClientRect().height;
    const opacity=reversing?Number(getComputedStyle(content).opacity):open?0:1;
    cancelMotion();
    content.inert=!open;
    // A background window can receive a media change event after the next click.
    if(matchMedia("(prefers-reduced-motion: reduce)").matches){
      delete el.dataset.folding;setRetained(open);return;
    }
    const to=open?el.getBoundingClientRect().height:closed;
    el.dataset.folding="";
    setRetained(true);
    const duration=180;
    const size=el.animate([{height:from+"px"},{height:to+"px"}],
      {duration,easing:"cubic-bezier(.25,.75,.25,1)",fill:"both"});
    const fade=content.animate([{opacity},{opacity:open?1:0}],{duration:150,easing:"ease-out",fill:"both"});
    motion.current=[size,fade];
    const settle=()=>{
      if(motion.current[0]!==size)return;
      // Close natively before clearing the animated height to avoid a one-frame rebound.
      el.open=open;
      delete el.dataset.folding;
      cancelMotion();setRetained(open);
    };
    // Electron can stop advancing the animation when the window is occluded.
    motionTimeout.current=window.setTimeout(settle,duration+90);
    void size.finished.then(settle,()=>{});
  },[open,reducedMotion]);
  useLayoutEffect(()=>cancelMotion,[]);
  return <details className={className} ref={root} open={present} data-expanded={open}
    data-live-step={live||undefined} onClick={e=>{
      const target=e.target as Element;
      if(target.closest("summary")!==root.current?.firstElementChild||target.closest("button,a,input,select"))return;
      e.preventDefault();onOpen(!open);
    }} onToggle={e=>{
      if(e.target===e.currentTarget&&e.currentTarget.open!==present)onOpen(e.currentTarget.open);
    }}>
    {summary}{children(present)}
  </details>;
}
