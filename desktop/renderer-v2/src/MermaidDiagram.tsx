import {useEffect, useLayoutEffect, useRef, useState} from "react";
import {useApplicationSettings} from "./ApplicationSettings";
import {CodeBlock} from "./CodeBlock";
import {ImageViewport} from "./ImageViewport";
import {captureDialogReturn} from "./useDialogFocus";
import {useUiLanguage} from "./UiLanguage";

const maxSourceLength = 8000;
let sequence = 0;
let library:Promise<typeof import("mermaid").default> | undefined;

function loadMermaid() {
  return library ??= import("mermaid").then(({default:mermaid}) => {
    mermaid.initialize({
      startOnLoad:false,
      securityLevel:"strict",
      htmlLabels:false,
      maxTextSize:maxSourceLength,
      maxEdges:100,
      suppressErrorRendering:true,
      fontFamily:"system-ui, 'Microsoft YaHei UI', sans-serif",
    });
    return mermaid;
  });
}

function canRender(source:string) {
  if (source.length > maxSourceLength) return false;
  // A chat flowchart has no configuration directives, remote assets or actions.
  if (/(?:%%|@)\s*\{|\burl\s*\(|@import|(?:^|[;\r\n])\s*click\s/i.test(source)) return false;
  const header = source.split(/\r?\n/).map(line => line.trim())
    .find(line => line && !line.startsWith("%%"));
  return /^(?:flowchart|graph)(?:\s|$)/.test(header ?? "");
}

async function renderFlowchart(source:string, theme:"dark"|"neutral") {
  const mermaid = await loadMermaid();
  const host = document.createElement("div");
  host.className = "message-flowchart-measure";
  document.body.append(host);
  try {
    // Only the host can set the theme. The source passed the chat flowchart checks.
    const themedSource = `---\nconfig:\n  theme: ${theme}\n---\n${source}`;
    const {svg} = await mermaid.render("modot-flowchart-" + ++sequence, themedSource, host);
    const root = new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
    const size = root.getAttribute("viewBox")?.trim().split(/\s+/).map(Number);
    if (root.localName !== "svg" || !size || size.length !== 4 ||
        !size.every(Number.isFinite) || size[2] <= 0 || size[3] <= 0) {
      throw Error("Invalid flowchart extent");
    }
    const width = Math.ceil(size[2]), height = Math.ceil(size[3]);
    root.setAttribute("width", String(width));
    root.setAttribute("height", String(height));
    // The browser displays an inert image. Mermaid event bindings never run in the message.
    return {url:"data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(root)),
      width, height};
  } finally {
    host.remove();
  }
}

export function MermaidDiagram({source}:{source:string}) {
  const {t} = useUiLanguage();
  const {theme} = useApplicationSettings();
  const diagramTheme = theme === "light" ? "neutral" : "dark";
  const anchor = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [zoom, setZoom] = useState(false);
  const [near, setNear] = useState(false);
  const [result, setResult] = useState<{source:string; image?:Awaited<ReturnType<typeof renderFlowchart>>}>();
  const allowed = canRender(source);
  useEffect(() => {
    if (near || !allowed || !anchor.current) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) setNear(true);
    }, {rootMargin:"160px"});
    observer.observe(anchor.current);
    return () => observer.disconnect();
  }, [near, allowed]);
  useEffect(() => {
    if (!allowed || !near) return;
    let active = true;
    void renderFlowchart(source, diagramTheme).then(
      image => {if (active) setResult({source, image});},
      () => {if (active) setResult({source});},
    );
    return () => {active = false;};
  }, [source, allowed, near, diagramTheme]);

  const current = result?.source === source ? result : undefined;
  const image = allowed ? current?.image : undefined;
  const expanded = !!image && zoom;
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!expanded || !element) return;
    const restore = captureDialogReturn();
    element.showModal();
    return () => {
      if (element.open) element.close();
      restore();
    };
  }, [expanded]);
  if (!allowed || current && !image) return <div className="message-flowchart" data-state="source">
    <p role="status">{t("流程图无法显示，已保留源码。")}</p>
    <CodeBlock text={source} language="mermaid"/>
  </div>;
  return <div className="message-flowchart" ref={anchor} data-state={image ? "rendered" : "pending"}>
    {image ? <button type="button" className="message-flowchart-view" aria-haspopup="dialog"
      aria-label={t("放大查看流程图")} title={t("放大查看流程图")} onClick={() => setZoom(true)}>
      <img src={image.url} alt={t("流程图")} width={image.width} height={image.height}
        onError={() => {setZoom(false); setResult({source});}}/>
    </button> : <p role="status">{t("正在渲染流程图…")}</p>}
    {image && expanded && <dialog className="conversation-image-dialog image-reader-dialog message-flowchart-dialog"
      ref={dialog} aria-label={t("流程图")} onCancel={event => event.stopPropagation()} onClose={() => setZoom(false)}>
      <header><strong>{t("流程图")}</strong><button type="button" onClick={() => dialog.current?.close()}>{t("关闭")}</button></header>
      <ImageViewport src={image.url} name={t("流程图")}/>
    </dialog>}
    <details>
      <summary>{t("查看 Mermaid 源码")}</summary>
      <CodeBlock text={source} language="mermaid"/>
    </details>
  </div>;
}
