import {createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode} from "react";
import {imageArtifact, useArtifactImage, useArtifactList} from "./artifactImages";
import {FileTypeIcon} from "./FileTypeIcon";
import type {Artifact} from "./generated";
import type {RetainedOutputReference} from "./resourceOpeners";
import {useUiLanguage} from "./UiLanguage";

type Owner = {
  conversationId:string;
  artifacts?:Artifact[];
  failed:boolean;
  load:()=>void;
  open?:OpenArtifact;
};
export type OpenArtifact = (id:string, versionId:string, imageLabel?:string)=>void;
const MessageArtifactOwner = createContext<Owner | undefined>(undefined);

export function MessageArtifacts({conversationId, open, children}:{
  conversationId?:string; open?:Owner["open"]; children:ReactNode;
}) {
  const [requested, setRequested] = useState<string>();
  const load = useCallback(() => setRequested(conversationId), [conversationId]);
  const enabled = !!conversationId && requested === conversationId;
  const list = useArtifactList(conversationId, enabled);
  const artifacts = list.error ? undefined : list.data?.artifacts;
  const failed = !!list.error;
  const owner = useMemo(() => conversationId ? {conversationId, artifacts, failed, load, open} : undefined,
    [conversationId, artifacts, failed, load, open]);
  return <MessageArtifactOwner.Provider value={owner}>{children}</MessageArtifactOwner.Provider>;
}

type Props = {reference:RetainedOutputReference; label:ReactNode; alt?:string; interactive?:boolean};

export function MessageArtifact(props:Props) {
  const owner = useContext(MessageArtifactOwner);
  if (!owner) return <span>{props.label}</span>;
  const key = JSON.stringify([owner.conversationId, props.reference.id, props.reference.versionId]);
  return <RetainedOutput key={key} owner={owner} {...props}/>;
}

function RetainedOutput({owner, reference, label, alt, interactive=true}:Props & {owner:Owner}) {
  const {conversationId, artifacts, failed, load, open} = owner;
  const {t} = useUiLanguage();
  const host = useRef<HTMLSpanElement>(null);
  const [seen, setSeen] = useState(false);
  const [brokenImage, setBrokenImage] = useState<string>();
  useEffect(() => {
    if (seen || !host.current) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      load();
      setSeen(true);
    }, {rootMargin:"160px"});
    observer.observe(host.current);
    return () => observer.disconnect();
  }, [seen, load]);

  const item = seen ? artifacts?.find(a => a.id === reference.id) : undefined;
  // Older retained-output links name only the artifact. Keep them on the
  // original file when a later correction appends another version.
  const candidate = reference.versionId
    ? item?.versions.find(v => v.id === reference.versionId)
    : item?.versions[0];
  const version = item?.permission === "read" && candidate?.state === "ready" ? candidate : undefined;
  const image = imageArtifact(version);
  const wantImage = alt !== undefined && image;
  const preview = useArtifactImage(conversationId, reference.id, wantImage ? version : undefined);
  const src = preview.src !== brokenImage ? preview.src : undefined;
  const name = version?.title ?? item?.title ?? t("成果");
  const extension = version?.extension ?? "";
  const fileName = name.endsWith(extension) ? name : name + extension;
  const figure = src ? <img alt={alt || name} src={src} decoding="async"
    onError={() => setBrokenImage(src)}/> : undefined;

  let status: string | undefined;
  if (seen) {
    if (failed || artifacts && !version) status = t("成果不可用");
    else if (!artifacts) status = t("正在读取…");
    else if (wantImage && !src) status = preview.pending ? t("正在读取图片…") : t("图片预览不可用");
  }
  return <span ref={host} className="message-artifact" data-picture={!!src || undefined}>
    {version && open && interactive ? <button type="button" className="message-artifact-open"
      aria-label={t(image ? "在右侧标签页查看 {name}" : "打开成果 {name}",{name:alt || name})}
      onClick={() => open(reference.id, version.id, image ? alt || name : undefined)}>
      {figure}
      <span className="message-artifact-label message-resource-link">
        <FileTypeIcon path={fileName} filled/>{label || name}
      </span>
    </button> : <>{figure}<span>{label || name}</span></>}
    {status && <small role="status">{status}</small>}
  </span>;
}
