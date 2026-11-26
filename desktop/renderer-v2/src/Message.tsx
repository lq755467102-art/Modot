// 呈现会话消息正文、代码块及安全的 Markdown 链接。
// 主要引用方包括 `Panels.tsx`。
import { createContext, isValidElement, memo, useContext, useLayoutEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { CodeBlock } from "./CodeBlock";
import { AnswerCitationLink } from "./AnswerCitation";
import { FileTypeIcon } from "./FileTypeIcon";
import { MessageArtifact } from "./MessageArtifact";
import { MermaidDiagram } from "./MermaidDiagram";
import { ResourceLinkOwner, useResourceLinks } from "./ResourceLinks";
import { parseResourceLink, parseRetainedOutput } from "./resourceOpeners";
import Markdown, { type Components, type ExtraProps, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Plugin } from "unified";
import type { Element, Root } from "hast";
import type { Text as MarkdownText } from "mdast";
import { appendMarkdown, type MarkdownStream, type MarkdownTree } from "./streamingMarkdown";
import { draftGlyphAge, proseFadeDuration, recordDraftPaint, type DraftPaint } from "./draftFade";
import './message-reading.css';

function textFragment(node: MarkdownText, start: number, end: number): MarkdownText {
  const fragment: MarkdownText = { type:"text", value:node.value.slice(start,end) };
  const source = node.position;
  const origin = source?.start.offset;
  if (!source || origin===undefined || source.end.offset!==origin+node.value.length) return fragment;
  const point = (index:number) => {
    const lines=node.value.slice(0,index).split(/\r\n|\r|\n/u);
    return {
      offset:origin+index,
      line:source.start.line+lines.length-1,
      column:lines.length===1?source.start.column+index:lines.at(-1)!.length+1,
    };
  };
  fragment.position={start:point(start),end:point(end)};
  return fragment;
}

/** Model output stays untrusted: no raw HTML, remote images, or executable links. */
function actionReferences() {
  return (tree: any) => {
    function walk(node: any) {
      if (!Array.isArray(node.children)) return;
      node.children = node.children.flatMap((child: any) => {
        if (child.type !== "text") {
          walk(child);
          return [child];
        }
        const parts: any[] = [];
        let last = 0;
        for (const match of child.value.matchAll(
          /\[\[action:([A-Za-z0-9_-]{1,128})\]\]/g,
        )) {
          if (match.index > last)
            parts.push(textFragment(child,last,match.index));
          parts.push({
            type: "link",
            url: "harness-action:" + match[1],
            children: [{ type: "text", value: "引用记录" }],
          });
          last = match.index + match[0].length;
        }
        // Ordinary prose still needs its parser offsets for the reveal clock.
        if (!last) return [child];
        if (last < child.value.length)
          parts.push(textFragment(child,last,child.value.length));
        return parts;
      });
    }
    walk(tree);
  };
}
const CodeStreaming=createContext(false);
const EvidenceAction=createContext<((id: string) => void) | undefined>(undefined);
function MarkdownTable({children}:{children?:ReactNode}) {
  const ref=useRef<HTMLDivElement>(null);
  const [overflow,setOverflow]=useState(false);
  useLayoutEffect(()=>{
    const view=ref.current!;const measure=()=>setOverflow(view.scrollWidth>view.clientWidth+1);
    measure();const observer=new ResizeObserver(measure);observer.observe(view);
    if(view.firstElementChild)observer.observe(view.firstElementChild);
    return()=>observer.disconnect();
  },[children]);
  return <div ref={ref} className="message-table-scroll" role={overflow?'region':undefined} aria-label={overflow?'表格，可横向滚动':undefined} tabIndex={overflow?0:undefined}>
    <table>{children}</table>
  </div>;
}
function MarkdownPre({children}:{children?:ReactNode}) {
  const streaming=useContext(CodeStreaming);
  if(!isValidElement<{className?:string;children?:ReactNode}>(children)||children.type!=="code")
    return <pre>{children}</pre>;
  const language=/^language-([^\s]+)/.exec(children.props.className||"")?.[1]||"";
  // remark appends one newline for the HTML pre element.
  const text=typeof children.props.children==="string"?children.props.children.replace(/\n$/,""):"";
  if (!streaming && language.toLowerCase()==="mermaid") return <MermaidDiagram source={text}/>;
  return <CodeBlock text={text} language={language} streaming={streaming}/>;
}

const InsideLink = createContext(false);

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const onEvidence=useContext(EvidenceAction);
  const owner=useContext(ResourceLinkOwner);
  const actions=useResourceLinks();
  if (!href) return <span>{children}</span>;
  if(href.startsWith("harness-action:"))return <AnswerCitationLink id={href.slice(15)} fallback={onEvidence}/>;
  const label = <InsideLink.Provider value={true}>{children}</InsideLink.Provider>;
  const reference=parseRetainedOutput(href);
  if(reference)return <MessageArtifact reference={reference} label={label}/>;
  const link=parseResourceLink(href);
  if(!link||link.kind==="file"&&!actions)return <span>{children}</span>;
  return <a className="message-resource-link" href={link.kind==="file"?"#":link.href} onClick={event=>{
    if(actions)actions.activate(event,link,owner);
    else {event.preventDefault();window.open(link.href,"_blank");}
  }} onContextMenu={actions?event=>actions.showMenu(event,link,owner):undefined}>
    {link.kind==="file"&&<FileTypeIcon path={link.path} filled/>}{label}
  </a>;
}

function MarkdownImage({src, alt}:{src?:string; alt?:string}) {
  const insideLink = useContext(InsideLink);
  const reference = parseRetainedOutput(src);
  if (reference) return <MessageArtifact reference={reference} label={alt} alt={alt ?? ""} interactive={!insideLink}/>;
  if (src && !insideLink) return <MarkdownLink href={src}>{alt || src}</MarkdownLink>;
  return <span>[图片：{alt || "未加载"}]</span>;
}

const graphemes = new Intl.Segmenter();
const literalBlocks = new Set(["a","code","pre","table"]);
const ProsePaint = createContext<DraftPaint | undefined>(undefined);

// Limit animation to a short, mutable prose tail. Code, tables and link
// controls retain the exact children their renderers expect.
const fadeDraftTail: Plugin<[number], Root> = origin => tree => {
  let remaining=16;
  function visit(parent:Root|Element) {
    // Keep the same text slots even after earlier text leaves the fade window.
    // React Markdown numbers siblings by tag, so removing an older slot can
    // remount an unrelated, still-fading text run later in the paragraph.
    for(let index=parent.children.length-1;index>=0;index--){
      const node=parent.children[index];
      if(node.type==="element"){
        if(literalBlocks.has(node.tagName)){
          remaining=0;
        } else {
          visit(node);
        }
      } else if(node.type==="text"){
        const offset=node.position?.start.offset;
        const length=node.value.length;
        parent.children[index]={
          type:"element",tagName:"span",
          properties:{
            dataFadeFrom:offset===undefined||!node.value.trim()?length:Math.max(0,length-remaining),
            ...(offset===undefined?{}:{dataPaintOffset:origin+offset}),
          },
          children:[node],
        };
        remaining=Math.max(0,remaining-length);
      }
    }
  }
  visit(tree);
};

function DraftGlyph({character, age}:{character:string;age:number}) {
  // Keep the initial delay for a mounted run. If a Markdown partition has
  // to remount it, the new instance resumes the same logical reveal clock.
  const [initialAge]=useState(age);
  if(initialAge>=proseFadeDuration)return character;
  return <span className="message-stream-fade" style={{animationDelay:-initialAge+"ms"}}>{character}</span>;
}

function MarkdownSpan({children,node,...props}:ComponentPropsWithoutRef<"span">&ExtraProps) {
  const paint=useContext(ProsePaint);
  const from=node?.properties.dataFadeFrom;
  if(typeof from!=="number"||typeof children!=="string")return <span {...props}>{children}</span>;
  const offset=node?.properties.dataPaintOffset;
  if(typeof offset!=="number"||from>=children.length)return children;
  const start=graphemes.segment(children).containing(from)?.index??from;
  return <>{children.slice(0,start)}
    {Array.from(graphemes.segment(children.slice(start)),({segment,index})=>
      <DraftGlyph key={start+index} character={segment} age={draftGlyphAge(paint,offset+start+index)}/>)}
  </>;
}

const components: Components = {
  pre: MarkdownPre,
  table: MarkdownTable,
  a: MarkdownLink,
  span: MarkdownSpan,
  img: MarkdownImage,
};
const ordinaryPlugins: Options["remarkPlugins"] = [remarkGfm];
const evidencePlugins: Options["remarkPlugins"] = [remarkGfm, actionReferences];
const useParsedTree: Plugin<[MarkdownTree], MarkdownTree> = function (tree) {
  // Keep the stored tree intact when remark plugins transform its nodes.
  this.parser = () => structuredClone(tree);
};

const MarkdownBody = memo(function MarkdownBody({ text, tree, streaming, evidence, fadeOrigin }: {
  text?: string; tree?: MarkdownTree; streaming: boolean; evidence: boolean; fadeOrigin?:number;
}) {
  const plugins = evidence ? evidencePlugins : ordinaryPlugins;
  const remarkPlugins: Options["remarkPlugins"] = tree ? [...plugins, [useParsedTree, tree]] : plugins;
  return <CodeStreaming.Provider value={streaming}><Markdown
    remarkPlugins={remarkPlugins}
    rehypePlugins={fadeOrigin===undefined?undefined:[[fadeDraftTail,fadeOrigin]]}
    skipHtml
    urlTransform={(url,key) => {
      if (parseRetainedOutput(url)) return url;
      if (key==="src") return parseResourceLink(url)?.kind==="file" ? url : "";
      if (key==="href" && (parseResourceLink(url) ||
          evidence && /^harness-action:[A-Za-z0-9_-]{1,128}$/.test(url))) return url;
      return "";
    }}
    components={components}
  >{text}</Markdown></CodeStreaming.Provider>;
});

export function Message({
  text,
  markdownSuffix="",
  onEvidence,
  streaming=false,
  fade=false,
}: {
  text: string;
  markdownSuffix?:string;
  streaming?:boolean;
  fade?:boolean;
  onEvidence?: (id: string) => void;
}) {
  const committed = useRef<MarkdownStream>(undefined);
  const committedPaint = useRef<DraftPaint>({source:"",ranges:[],now:0});
  const live = streaming ? appendMarkdown(committed.current, text, markdownSuffix) : undefined;
  const paint = recordDraftPaint(committedPaint.current,text,fade,fade?performance.now():0);
  useLayoutEffect(() => {
    committed.current = live;
    committedPaint.current = paint;
  }, [live,paint]);
  const evidence = !!onEvidence;
  const stable = live?.stable;
  const stableBodies = useMemo(() => stable?.map(part =>
    <MarkdownBody key={part.start} tree={part.tree} streaming={false} evidence={evidence}/>
  ) ?? [], [stable, evidence]);
  return <EvidenceAction.Provider value={onEvidence}><ProsePaint.Provider value={fade?paint:undefined}><div className="message-reading">
    {live ? [...stableBodies,
      <MarkdownBody key={live.tail.start} tree={live.tail.tree} streaming evidence={evidence}
        fadeOrigin={fade?live.tail.origin:undefined}/>
    ] : <MarkdownBody key={0} text={text} streaming={false} evidence={evidence}/>}
  </div></ProsePaint.Provider></EvidenceAction.Provider>;
}
