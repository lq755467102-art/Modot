// 渲染输入区的附件条和已发送消息的附件图片。
// 主要引用方包括 `AgentHome.tsx` 和 `ConversationTimeline.tsx`。
import {useEffect,useRef,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {api} from "./client";
import type {MessageAttachment} from "./generated";
import {ImageViewport} from "./ImageViewport";
import {RetainedDialog} from "./RetainedDialog";
import {UiIcon} from "./UiIcon";
import {useUiLanguage} from "./UiLanguage";

export const attachmentImageKey=(conversationId:string)=>["attachment-image",conversationId] as const;
function useAttachmentImage(conversationId:string,id:string,thumbnail:boolean,enabled:boolean) {
  return useQuery({
    queryKey:[...attachmentImageKey(conversationId),id,thumbnail],
    queryFn:()=>api<{dataUrl:string}>("/api/v2/conversations/threads/"+encodeURIComponent(conversationId)+"/attachments",{
      preview:id, ...(thumbnail?{thumbnail:true}:{})
    }),
    enabled,retry:false,gcTime:0,
  });
}

function AttachmentTile({conversationId,item,busy,onRemove,onPreview}:{
  conversationId:string;item:MessageAttachment;busy?:boolean;onRemove?:(id:string)=>void;
  onPreview:(item:MessageAttachment)=>void;
}) {
  const {t}=useUiLanguage();
  const tile=useRef<HTMLDivElement>(null);
  const [seen,setSeen]=useState(!!onRemove);
  const isImage=item.mime.startsWith("image/");
  const thumbnail=useAttachmentImage(conversationId,item.id,true,seen&&isImage);
  useEffect(()=>{
    if(!isImage||seen||!tile.current)return;
    const observer=new IntersectionObserver(entries=>{
      if(entries.some(entry=>entry.isIntersecting)){setSeen(true);observer.disconnect();}
    });
    observer.observe(tile.current);
    return()=>observer.disconnect();
  },[isImage,seen]);
  return <div ref={tile} className="message-attachment" data-attachment-id={item.id}>
    {isImage
      ? <button type="button" className="attachment-thumbnail" title={item.name}
          aria-label={t("预览图片 {name}",{name:item.name})} onClick={()=>onPreview(item)}>
          {thumbnail.data&&!thumbnail.error?<img src={thumbnail.data.dataUrl} alt=""/>:<UiIcon name="image"/>}
        </button>
      : <UiIcon name="document"/>}
    <span title={item.name}>{item.name}</span>
    {onRemove&&<button type="button" className="attachment-remove" disabled={busy}
      aria-label={t("移除附件 {name}",{name:item.name})} onClick={()=>onRemove(item.id)}><UiIcon name="close"/></button>}
  </div>;
}

export function MessageAttachments({conversationId,items,busy,onRemove}:{
  conversationId:string;items:readonly MessageAttachment[];busy?:boolean;onRemove?:(id:string)=>void;
}) {
  const {t}=useUiLanguage();
  const [preview,setPreview]=useState<MessageAttachment>();
  const selected=preview&&items.find(item=>item.id===preview.id);
  const original=useAttachmentImage(conversationId,selected?.id??"",false,!!selected);
  if(!items.length)return null;
  return <div className="message-attachments" aria-label={t("消息附件")}>
    {items.map(item=><AttachmentTile key={item.id} conversationId={conversationId} item={item}
      busy={busy} onRemove={onRemove} onPreview={setPreview}/>)}
    {selected&&<RetainedDialog label={selected.name} open={true} onClose={()=>setPreview(undefined)}>
      {original.error?<p role="alert">{t("图片原件不可用。")}</p>:original.data
        ? <ImageViewport key={selected.id} name={selected.name} src={original.data.dataUrl}/>
        : <p role="status">{t("正在读取图片…")}</p>}
    </RetainedDialog>}
  </div>;
}
