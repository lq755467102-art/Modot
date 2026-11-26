// 在插件管理中展示目录 Skill 的详情、安装状态以及交给会话使用的入口。
// 主要引用方包括 `PluginManagementShell.tsx`。
import {useLayoutEffect, useRef, useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {ExtensionDetails} from './ExtensionCard';
import {UiIcon} from './UiIcon';
import {installationIsCurrent, readSkillDraft, refreshInstalledCatalog, skillSourceLabel, type CatalogSkill, type SkillDraft} from './extensionCatalog';
import type {ToolHandoff} from './ToolHandoff';
import {useUiLanguage} from './UiLanguage';

// 本地 Skill 预览使用当前会话和 `resourceId`；安装包 Skill 的可用性依据 `catalogReady` 核对。
type Props = {
  skill: CatalogSkill; conversationId: string; resourceId: string; catalogReady: boolean;
  onPackage: (id: string) => void; onBack: () => void; onUse: (draft: ToolHandoff, owner: string) => void;
};

// `PluginManagementShell.tsx` 提供会话、资源和技能修订。
// 页面只预览或生成会话草稿，实际提交交回上层。
export function CatalogSkillDetail({skill, conversationId, resourceId, catalogReady, onPackage, onBack, onUse}: Props) {
  const {t} = useUiLanguage();
  const [preview, setPreview] = useState<SkillDraft>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(false);
  const pending = useRef(false);
  const query = useQueryClient();
  const reloadOnLeave = useRef(false);
  // The parent keys this screen by the conversation, source and skill revision.
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (reloadOnLeave.current) void query.invalidateQueries({queryKey:skill.pkg ? ['plugins'] : ['manual-skills', conversationId, resourceId]});
    };
  }, [query, conversationId, resourceId, skill.pkg?.id]);

  const waitingForCatalog = !!skill.pkg && !catalogReady;
  const blocked = skill.unsupported.length ? '未适配的能力 · ' + skill.unsupported.join('、')
    : !skill.manual ? '此技能未开放手动调用。'
    : skill.pkg && !skill.pkg.skillsEnabled ? '请先在所属插件的管理页启用技能。' : '';

  // `extensionCatalog.ts` 读取已选 Skill。
  // 加入草稿前复取内容，安装状态或文字版本变化时要求重新核对。
  async function load(apply: boolean) {
    if (pending.current || blocked || (skill.pkg && !installationIsCurrent(query, skill.pkg))) return;
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      const draft = await readSkillDraft(skill, conversationId, resourceId);
      if (!mounted.current) return;
      if (skill.pkg && !installationIsCurrent(query, skill.pkg)) {
        setPreview(undefined);
        setError('插件安装状态已变化，请重新打开技能预览。');
        return;
      }
      if (!apply) setPreview(draft);
      else if (draft.version !== preview?.version || draft.text !== preview.text) {
        setPreview(draft);
        setError('技能内容已更新，请核对新预览后再加入草稿。');
      } else {
        setPreview(undefined);
        onUse(draft, conversationId);
      }
    } catch (reason) {
      if (mounted.current) {
        if (skill.pkg) void refreshInstalledCatalog(query);
        else reloadOnLeave.current = true;
        setPreview(undefined);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return <article className="catalog-detail" data-skill-source={skill.group}>
    <header className="catalog-identity"><span className="catalog-icon"><UiIcon name="model"/></span>
      <div><h1 tabIndex={-1} data-catalog-heading>{skill.name}</h1><small>{skillSourceLabel(skill,t)}</small></div>
    </header>
    <p className="catalog-description">{skill.description || '此技能尚未提供用途说明。'}</p>
    <dl className="catalog-facts">
      {skill.pkg && <div><dt>{t('所属插件')}</dt><dd><button className="catalog-link" onClick={() => onPackage(skill.pkg!.id!)}>{skill.pkg.name} <UiIcon name="open"/></button></dd></div>}
      <div><dt>调用方式</dt><dd>{[skill.manual && '手动加入草稿', skill.automatic && 'Modot按需调用'].filter(Boolean).join(' · ') || '未开放调用'}</dd></div>
      <div><dt>效果示例</dt><dd>此技能尚无可展示的输入和输出示例。</dd></div>
    </dl>
    {!!skill.pkg?.servers.length && <p className="catalog-note">{t('若技能说明用到 MCP 工具，可在插件的「管理」中启用对应的 MCP 服务。')}</p>}
    <button className="catalog-primary" disabled={busy || waitingForCatalog || !!blocked} onClick={() => void load(false)}>{busy && !preview ? '正在读取…' : '预览草稿'}</button>
    {blocked && <p className="catalog-note">{blocked}</p>}
    {error && !preview && <p role="alert">{error} <button onClick={onBack}>返回并刷新</button></p>}
    {preview && <ExtensionDetails title="草稿预览" busy={busy} onClose={() => setPreview(undefined)}>
      <p>{preview.title}</p><pre className="catalog-draft">{preview.text}</pre>
      {error && <p role="alert">{error}</p>}
      <button className="catalog-primary" disabled={busy || waitingForCatalog} onClick={() => void load(true)}>{busy ? '正在核对…' : '加入当前草稿'}</button>
    </ExtensionDetails>}
  </article>;
}
