// 处理证据图片查看时的缩放、平移与视图重置。
// 主要引用方包括 `ConversationImageCard.tsx`。
import {useLayoutEffect,useRef,useState} from 'react';
import './image-viewer.css';

/** Reading controls consume an already-authorized image; they never fetch it. */
export function ImageViewport({src,name}:{src:string;name:string}){
  const viewport=useRef<HTMLDivElement>(null);
  const [natural,setNatural]=useState({width:0,height:0}),[box,setBox]=useState({width:0,height:0});
  const [mode,setMode]=useState<'fit'|'manual'>('fit'),[manual,setManual]=useState(1),[failed,setFailed]=useState(false),[dragging,setDragging]=useState(false);
  const drag=useRef<{pointer:number;x:number;y:number;left:number;top:number}|undefined>(undefined);
  const center=useRef<{x:number;y:number}|undefined>(undefined);
  useLayoutEffect(()=>{
    const el=viewport.current!;
    const measure=(width:number,height:number)=>setBox(old=>old.width===width&&old.height===height?old:{width,height});
    measure(el.clientWidth,el.clientHeight);
    // Integer client sizes can round up at browser zoom and create scrollbar loops.
    const observer=new ResizeObserver(([entry])=>measure(entry.contentRect.width,entry.contentRect.height));observer.observe(el);return()=>observer.disconnect();
  },[]);
  const ready=natural.width>0&&natural.height>0&&box.width>0&&box.height>0;
  const fit=ready?Math.min(1,box.width/natural.width,box.height/natural.height):1;
  const minimum=Math.min(.1,fit),scale=mode==='fit'?fit:Math.max(minimum,Math.min(8,manual));
  const width=natural.width*scale,height=natural.height*scale;
  useLayoutEffect(()=>{
    const el=viewport.current!;
    if(mode==='fit'){el.scrollLeft=0;el.scrollTop=0;center.current=undefined;}
    else if(center.current){
      el.scrollLeft=center.current.x*scale+Math.max(0,(box.width-width)/2)-box.width/2;
      el.scrollTop=center.current.y*scale+Math.max(0,(box.height-height)/2)-box.height/2;
      center.current=undefined;
    }
  },[scale,mode,box.width,box.height,width,height]);
  function zoom(value:number){
    const el=viewport.current!;
    center.current={x:(el.scrollLeft+box.width/2-Math.max(0,(box.width-width)/2))/scale,y:(el.scrollTop+box.height/2-Math.max(0,(box.height-height)/2))/scale};
    setManual(Math.max(minimum,Math.min(8,value)));setMode('manual');
  }
  function reset(){center.current=undefined;setMode('fit');if(viewport.current){viewport.current.scrollLeft=0;viewport.current.scrollTop=0;}}
  function endDrag(){drag.current=undefined;setDragging(false);}
  return <section className="image-reader" aria-label="原图阅读">
    <div className="image-view-controls" role="group" aria-label="图像阅读控制">
      <button type="button" disabled={!ready} aria-pressed={mode==='fit'} onClick={reset}>适应窗口</button>
      <button type="button" disabled={!ready} onClick={()=>zoom(1)}>100%</button>
      <button type="button" disabled={!ready||scale>=8} onClick={()=>zoom(scale*1.25)}>放大</button>
      <button type="button" disabled={!ready||scale<=minimum} onClick={()=>zoom(scale/1.25)}>缩小</button>
      <button type="button" disabled={!ready} onClick={reset}>复位</button>
      <span role="status" aria-atomic="true">{ready?`${(scale*100).toFixed(scale<.1?1:0)}% · ${mode==='fit'?'适应窗口':'手动缩放'}`:'正在准备图像…'}</span>
    </div>
    {failed&&<p role="alert">无法显示这张图片。请关闭后重新打开。</p>}
    <div ref={viewport} className={'image-viewport'+(dragging?' is-dragging':'')} role="region" aria-label="原图阅读区" tabIndex={0}
      onPointerDown={event=>{
        const el=event.currentTarget;if(event.button!==0||event.ctrlKey||event.metaKey||(el.scrollWidth<=el.clientWidth&&el.scrollHeight<=el.clientHeight))return;
        event.preventDefault();el.focus({preventScroll:true});el.setPointerCapture(event.pointerId);drag.current={pointer:event.pointerId,x:event.clientX,y:event.clientY,left:el.scrollLeft,top:el.scrollTop};setDragging(true);
      }}
      onPointerMove={event=>{const start=drag.current;if(start?.pointer===event.pointerId){event.currentTarget.scrollLeft=start.left-(event.clientX-start.x);event.currentTarget.scrollTop=start.top-(event.clientY-start.y);}}}
      onPointerUp={event=>{endDrag();if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);}}
      onPointerCancel={endDrag} onLostPointerCapture={endDrag}>
      <div className="image-viewport-stage" style={{width:mode==='fit'?'100%':Math.max(box.width,width),height:mode==='fit'?'100%':Math.max(box.height,height)}}>
        <img src={src} alt={name} draggable={false} style={ready?{width,height}:undefined} onLoad={event=>setNatural({width:event.currentTarget.naturalWidth,height:event.currentTarget.naturalHeight})} onError={()=>setFailed(true)}/>
      </div>
    </div>
    {ready&&<small className="image-reader-hint">{natural.width} × {natural.height} · 放大后可拖动，或聚焦阅读区用方向键移动。</small>}
  </section>;
}
