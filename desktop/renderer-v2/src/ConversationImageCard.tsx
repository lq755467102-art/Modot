// 沿已保存的图片证据或成果版本显示预览，并提供放大和审查入口。
// 主要引用方包括 `ConversationTimeline.tsx` 和 `EvidenceInspector.tsx`。
import {useEffect, useLayoutEffect, useRef, useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api, queryString} from "./client";
import {ImageViewport} from './ImageViewport';
import {captureDialogReturn} from './useDialogFocus';

// `image_evidence.py` 返回的图片信息。
// `hash` 固定图片内容，`artifactId` 与 `versionId` 一起定位成果，`modelDelivered` 表示原动作是否有模型送达记录。
type ImageMetadata = {name: string; hash: string; width?: number; height?: number; artifactId?: string; versionId?: string; edited?: boolean; modelDelivered?: boolean};
type ImagePreview = ImageMetadata & {dataUrl: string};

// 使用时间线传入的原动作身份。
// 可见时先轮询当前可读性，再用相同哈希的缩略图和原图；撤权或读回失败时收起预览。
export function ConversationImageCard({conversation, action, version, title, state, queued, onArtifact}: {
  conversation: string; action: string; version?: string; title: string; state: unknown; queued?: boolean;
  onArtifact?: (id: string, version: string) => void;
}) {
  const card = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const zoomButton = useRef<HTMLButtonElement>(null);
  const [inView, setInView] = useState(false);
  const [zoom, setZoom] = useState(false);
  const [zoomRequest, setZoomRequest] = useState(0);
  const route = `/api/v2/conversations/threads/${queryString(conversation)}/evidence/${queryString(action)}/image`;
  useEffect(() => {
    if (!card.current) return;
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), {rootMargin:"160px"});
    observer.observe(card.current);
    return () => observer.disconnect();
  }, []);
  const enabled = !!version && state === "completed" && (inView || zoom);
  const authority = useQuery({queryKey:["action-image-authority", conversation, action, version],
    queryFn:()=>api<ImageMetadata>(route + "?meta=1"), enabled, retry:false, gcTime:0,
    refetchInterval:enabled ? 3000 : false, refetchIntervalInBackground:true});
  const metadata = enabled && !authority.error ? authority.data : undefined;
  const thumb = useQuery({queryKey:["action-image", conversation, action, version, metadata?.hash],
    queryFn:()=>api<ImagePreview>(route), enabled:!!metadata, retry:false, gcTime:0, staleTime:Infinity});
  const preview = !thumb.error && thumb.data?.hash === metadata?.hash ? thumb.data : undefined;
  const large = useQuery({queryKey:["action-image-full", conversation, action, version, metadata?.hash, zoomRequest],
    queryFn:()=>api<ImagePreview>(route + "?full=1"), enabled:!!preview && zoom, retry:false, gcTime:0, staleTime:Infinity});
  const full = !large.error && large.data?.hash === preview?.hash ? large.data : undefined;
  const modal = !!preview && zoom;
  useEffect(() => {
    if (zoom && (!version || authority.error || thumb.error)) setZoom(false);
  }, [modal, zoom, version, authority.error, thumb.error]);
  useLayoutEffect(()=>{
    const el=dialog.current;if(!modal||!el)return;
    const restore=captureDialogReturn();if(!el.open)el.showModal();
    return()=>{if(el.open)el.close();restore();};
  },[modal]);
  const close = () => {setZoom(false);};
  return <div ref={card} className="conversation-image-preview" aria-label={title}>
    {preview && <>
      <p>{preview.edited && "编辑图片 · "}{preview.name}{queued || metadata?.modelDelivered ? " · 已交给模型" : title === "查看图片" ? " · 已读取" : ""}</p>
      <button ref={zoomButton} type="button" className="conversation-image-zoom" aria-label={`放大 ${preview.name}`} onClick={()=>{setZoomRequest(n=>n+1);setZoom(true);}}>
        <img alt={preview.name} src={preview.dataUrl}/>
      </button>
      {preview.artifactId && preview.versionId && onArtifact &&
        <button type="button" onClick={()=>onArtifact(preview.artifactId!,preview.versionId!)}>查看成果版本</button>}
    </>}
    {state === "failed" && <small>这次图片读取未成功，可在审查中核对原因。</small>}
    {state !== "completed" && state !== "failed" && <small>图片读取尚未确认。</small>}
    {enabled && !authority.error && (authority.isPending || thumb.isPending) && <small role="status">正在读取图片预览…</small>}
    {enabled && (authority.error || thumb.error) && <small role="status">{(authority.error || thumb.error)?.message}</small>}
    {modal && <dialog className="conversation-image-dialog image-reader-dialog" ref={dialog} aria-label="大图预览" onCancel={e=>e.stopPropagation()} onClose={close}>
      <header><strong>{preview.name}</strong><button type="button" onClick={()=>dialog.current?.close()}>关闭大图</button></header>
      {large.error ? <p role="alert">{large.error.message}</p> : full ? <ImageViewport key={JSON.stringify([conversation,action,version,full.hash])} src={full.dataUrl} name={full.name}/> : <p role="status">正在读取原图…</p>}
    </dialog>}
  </div>;
}
