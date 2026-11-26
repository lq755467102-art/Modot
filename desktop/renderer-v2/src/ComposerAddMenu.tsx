// 把图片、已有附件、Skill 或 MCP 来源加入当前会话草稿。
// 主要引用方包括 `AgentHome.tsx`。
import {useEffect, useLayoutEffect, useRef, useState} from "react";
import {useQueryClient} from "@tanstack/react-query";
import {
  installationIsCurrent, localSkill, packageSkill, readSkillDraft, refreshInstalledCatalog, skillSourceLabel,
  useInstalledPackages, useLocalSkills, type CatalogSkill,
} from "./extensionCatalog";
import type {Attachment} from "./generated";
import {githubConnection} from "./githubIntegration";
import type {WriteActiveDraft} from "./ToolHandoff";
import {UiIcon} from "./UiIcon";
import {useUiLanguage} from "./UiLanguage";

type Props = {
  owner: string;
  active: boolean;
  disabled: boolean;
  attachmentsBusy: boolean;
  scopeRevision?: string;
  existingAttachments: readonly Pick<Attachment, "id" | "name" | "mime">[];
  onPickFiles: (accept: string) => void;
  onAttach: (id: string) => void;
  onAppend: WriteActiveDraft;
};

export function ComposerAddMenu({
  owner, active, disabled, attachmentsBusy, scopeRevision,
  existingAttachments, onPickFiles, onAttach, onAppend,
}: Props) {
  const {t}=useUiLanguage();
  const client=useQueryClient();
  const [open,setOpen]=useState(false);
  const [resourceId,setResourceId]=useState("");
  const [pending,setPending]=useState(false);
  const [error,setError]=useState("");
  const container=useRef<HTMLDivElement>(null);
  const trigger=useRef<HTMLButtonElement>(null);
  const panel=useRef<HTMLDivElement>(null);
  const operation=useRef<AbortController>(null);
  const visible=open && active && !disabled;
  const catalog=useInstalledPackages(visible);
  const defaultSkills=useLocalSkills(owner,"",visible);
  const scopedSkills=useLocalSkills(owner,resourceId,visible && !!resourceId);
  const locals=resourceId ? scopedSkills : defaultSkills;
  const sources=defaultSkills.data?.sources ?? [];
  const directory=(sources.find(source=>source.id===resourceId) ?? (sources.length===1 ? sources[0] : undefined))?.name;
  const personalAndProjectSkills=(visible && !locals.error ? locals.data?.skills ?? [] : []).map(skill=>localSkill(skill,directory));
  const pluginSkills=visible && catalog.complete ? catalog.packages.flatMap(pkg=>pkg.skillsEnabled
    ? pkg.skills.map(skill=>packageSkill(pkg,skill)) : []) : [];
  const skills=[...personalAndProjectSkills,...pluginSkills].filter(skill=>skill.manual && !skill.unsupported.length);
  const pluginServices=visible && catalog.complete ? catalog.packages.flatMap(pkg=>pkg.servers
    .filter(server=>pkg.remoteEnabled && server.supported && (!githubConnection(pkg,server) || server.connected))
    .map(server=>({pkg,server}))) : [];

  function close(restoreFocus = false) {
    operation.current?.abort();
    operation.current=null;
    setOpen(false);
    setPending(false);
    setError("");
    if (restoreFocus) trigger.current?.focus();
  }

  useLayoutEffect(()=>{
    close();
    setResourceId("");
    return ()=>operation.current?.abort();
  },[owner,scopeRevision,active,disabled]);
  useEffect(()=>{
    if (!visible) return;
    const outside=(event:PointerEvent)=>{
      if (!container.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown",outside);
    return ()=>document.removeEventListener("pointerdown",outside);
  },[visible]);

  function append(text: string) {
    onAppend(owner,text);
    close();
  }

  async function addSkill(skill: CatalogSkill) {
    if (operation.current) return;
    const controller=new AbortController();
    operation.current=controller;
    setPending(true);
    setError("");
    try {
      const verifyInstallation=()=>{
        if (skill.pkg && !installationIsCurrent(client,skill.pkg))
          throw Error(t("插件状态已变化，请重新选择。"));
      };
      verifyInstallation();
      const draft=await readSkillDraft(skill,owner,resourceId);
      if (controller.signal.aborted || container.current?.closest("[hidden],[inert]")) return;
      verifyInstallation();
      append(draft.text);
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (skill.pkg) void refreshInstalledCatalog(client);
      else void locals.refetch();
      setError(reason instanceof Error ? reason.message : t("无法添加技能。"));
    } finally {
      if (operation.current===controller) {
        operation.current=null;
        setPending(false);
      }
    }
  }

  const addFiles=(accept:string)=>{close(true);onPickFiles(accept);};
  return <div className="composer-add" ref={container}
    onBlur={event=>{
      if (visible && event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) close();
    }}
    onKeyDown={event=>{
      if (!visible || event.key!=="Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close(true);
    }}>
    <button ref={trigger} type="button" id="plus" className="plus" disabled={disabled || !active}
      title={t("添加到消息")} aria-label={t("添加到消息")} aria-expanded={visible}
      aria-controls={visible ? "composer-add-actions" : undefined} onClick={()=>{
        if (visible) close(true);
        else {
          setOpen(true);
          requestAnimationFrame(()=>panel.current?.querySelector<HTMLElement>("button:enabled, select:enabled")?.focus({preventScroll:true}));
        }
      }}><UiIcon name="plus"/></button>
    {visible&&<div ref={panel} id="composer-add-actions" className="composer-add-actions" role="group" aria-label={t("添加到消息")}>
      <section aria-label={t("附件")}>
        <h3>{t("附件")}</h3>
        <button type="button" disabled={attachmentsBusy || pending} onClick={()=>addFiles(".png,.jpg,.jpeg")}>
          <UiIcon name="image"/><span>{t("添加图片")}<small>PNG · JPEG</small></span>
        </button>
        <button type="button" disabled={attachmentsBusy || pending} onClick={()=>addFiles(".mp4,.webm")}>
          <UiIcon name="image"/><span>{t("添加视频")}<small>MP4 · WebM</small></span>
        </button>
        <button type="button" disabled={attachmentsBusy || pending} onClick={()=>addFiles(".txt,.md,.pdf,.docx,.csv,.tsv,.xlsx")}>
          <UiIcon name="document"/><span>{t("添加文档")}<small>TXT · Markdown · PDF · DOCX · CSV · TSV · XLSX</small></span>
        </button>
      </section>
      <section aria-label={t("技能")}>
        <h3>{t("技能")}</h3>
        {sources.length>1&&<label>{t("技能来源")}
          <select value={resourceId} disabled={pending} onChange={event=>{setResourceId(event.target.value);setError("");}}>
            <option value="">{t("个人技能")}</option>
            {sources.map(source=><option key={source.id} value={source.id}>{source.name}</option>)}
          </select>
        </label>}
        {skills.map(skill=><button key={skill.key} type="button" title={skill.description || undefined}
          disabled={pending} onClick={()=>void addSkill(skill)}>
          <UiIcon name="model"/><span>{skill.name}<small>{skillSourceLabel(skill,t)}</small></span>
        </button>)}
        {locals.error&&<p className="composer-add-status" role="status">{t("无法读取本地技能。")}</p>}
        {!skills.length && !locals.error && !catalog.error && <p className="composer-add-status" role="status">
          {t(locals.isFetching || !catalog.complete ? "正在读取技能…" : "当前没有可手动添加的技能。")}
        </p>}
      </section>
      <section aria-label={t("MCP 服务")}>
        <h3>{t("MCP 服务")}</h3>
        {pluginServices.map(({pkg,server})=><button key={pkg.id+"/"+server.name} type="button" disabled={pending} onClick={()=>{
          try {
            if (!installationIsCurrent(client,pkg)) throw Error(t("插件状态已变化，请重新选择。"));
            append(t("请使用已选插件 {name} 的 MCP 服务 {server}（安装 ID {id}，版本 {version}）。先核对本轮的授权和可用工具。",{
              name:pkg.name, server:server.name, id:pkg.id, version:pkg.version,
            }));
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : t("无法添加插件能力。"));
          }
        }}><UiIcon name="extensions"/><span>{pkg.name}<small>{server.name} · MCP · {pkg.version}</small></span></button>)}
        {!pluginServices.length&&<p className="composer-add-status" role="status">
          {t(catalog.error ? "无法读取插件目录。" : catalog.complete ? "当前没有可添加的 MCP 能力。" : "正在读取插件目录…")}
        </p>}
      </section>
      {!!existingAttachments.length&&<section aria-label={t("已有附件")}>
        <h3>{t("已有附件")}</h3>
        {existingAttachments.map(item=><button key={item.id} type="button" title={item.name} disabled={attachmentsBusy || pending}
          onClick={()=>{onAttach(item.id);close(true);}}>
          <UiIcon name={item.mime.startsWith("image/") ? "image" : "document"}/><span>{item.name}</span>
        </button>)}
      </section>}
      {pending&&<p className="composer-add-status" role="status">{t("正在添加技能…")}</p>}
      {error&&<p className="composer-add-status" role="alert">{error}</p>}
    </div>}
  </div>;
}
