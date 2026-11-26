// 封装已安装插件与目录 Skill 的查询、刷新、安装身份和分页。
// 主要引用方包括 `usePluginLogin.ts` 和 `CatalogPackageManagement.tsx`。
import {useQuery, useQueryClient, type QueryClient} from '@tanstack/react-query';
import {api, APIError} from './client';
import type {InstalledContributionPackage, InstalledContributionPage, PluginContributionIcon, PluginInstallRevision, PluginPackage, PluginPage, PluginSkill} from './generated';
import type {ToolHandoff} from './ToolHandoff';
import type {UiTranslator} from './uiMessages';

export const pluginEndpoint = '/api/v2/plugins';
export const installedCatalogKey = ['plugins', 'catalog'] as const;
export type CatalogPackage = PluginPackage & InstalledContributionPackage;

// 同时比较包 ID、内容指纹和状态修订，以确认预览或动作仍指向原安装。
export function sameInstallation(left: PluginInstallRevision, right: PluginInstallRevision) {
  return left.id === right.id && left.fingerprint === right.fingerprint && left.revision === right.revision;
}

// 把分页不一致、游标异常和安装变化统一交给整目录重读。
function catalogChanged() {
  return new APIError(409, 'plugin_catalog_stale', '读取期间插件目录发生了变化。');
}

async function readPackageDetails(signal: AbortSignal) {
  // The older list still supplies descriptions, connection labels and Skill details.
  const details = new Map<string, PluginPackage>();
  let offset = 0;
  let total: number | undefined;
  while (true) {
    signal.throwIfAborted();
    const page = await api<PluginPage>(pluginEndpoint + '?after=' + offset);
    total ??= page.total;
    if (page.total !== total) throw catalogChanged();
    for (const pkg of page.plugins) {
      if (!pkg.id || pkg.removed || details.has(pkg.id)) throw catalogChanged();
      details.set(pkg.id, pkg);
    }
    if (!page.hasMore) break;
    if (!page.plugins.length || page.next <= offset || details.size >= total) throw catalogChanged();
    offset = page.next;
  }
  if (details.size !== total) throw catalogChanged();
  return details;
}

// 从 Python 插件宿主读取目录快照，以安装指纹和修订合并旧列表的详情。
// 仅完整且同一时点的结果进入缓存。
async function readInstalledCatalog(signal: AbortSignal) {
  const details = await readPackageDetails(signal);
  const total = details.size;
  const packages: CatalogPackage[] = [];
  const cursors = new Set<string>();
  let snapshot: string | undefined;
  let cursor: string | null = null;
  do {
    signal.throwIfAborted();
    const suffix = cursor === null ? '' : '?cursor=' + encodeURIComponent(cursor);
    const page: InstalledContributionPage = await api(pluginEndpoint + '/contributions' + suffix);
    signal.throwIfAborted();
    snapshot ??= page.snapshot;
    if (page.snapshot !== snapshot || page.total !== total || page.hasMore !== (page.nextCursor !== null)) throw catalogChanged();
    for (const installed of page.plugins) {
      const pkg = details.get(installed.id);
      if (!installed.installed || !pkg || pkg.fingerprint !== installed.fingerprint || pkg.revision !== installed.revision) throw catalogChanged();
      packages.push({...pkg, ...installed});
      details.delete(installed.id);
    }
    cursor = page.nextCursor;
    if (cursor !== null) {
      if (!page.plugins.length || !details.size || cursors.has(cursor)) throw catalogChanged();
      cursors.add(cursor);
    }
  } while (cursor !== null);
  if (details.size) throw catalogChanged();
  return packages;
}

// 为管理界面提供已核对的安装清单与整体读取状态；中间页不进入共享缓存。
export function useInstalledPackages(visible: boolean) {
  const query = useQuery({
    queryKey: installedCatalogKey,
    enabled: visible,
    // Retry the entire read on expiry. Intermediate pages never enter the cache.
    queryFn: ({signal}) => readInstalledCatalog(signal),
    retry: 1,
  });
  return {
    packages: query.data ?? [],
    complete: query.isSuccess && query.fetchStatus === 'idle',
    error: query.error?.message ?? '',
    retry: query.refetch,
  };
}

export type IconInstallation = Pick<InstalledContributionPackage, 'id' | 'fingerprint' | 'revision' | 'iconThemes'>;

// 同一安装修订的包图按主题共享，目录重读期间先显示缺省图标。
export function useInstalledPackageIcon(installation: IconInstallation, theme: string, ready: boolean) {
  const client = useQueryClient();
  const {id, fingerprint, revision, iconThemes} = installation;
  const variant = theme === 'light' || theme === 'dark' ? theme : 'default';
  const hasVariant = iconThemes?.includes(variant) || iconThemes?.includes('default');
  const image = useQuery({
    queryKey: ['installed-package-icon', id, fingerprint, revision, variant],
    enabled: ready && !!hasVariant,
    staleTime: Infinity,
    retry: false,
    queryFn: async ({signal}) => {
      try {
        const params = new URLSearchParams({fingerprint, revision:String(revision), theme:variant});
        const result = await api<PluginContributionIcon>(pluginEndpoint + '/contributions/' + encodeURIComponent(id) + '/icon?' + params);
        signal.throwIfAborted();
        if (!sameInstallation(result, installation) || result.theme !== variant) {
          throw catalogChanged();
        }
        if (result.resolvedTheme !== variant && result.resolvedTheme !== 'default') return null;
        if (result.mime !== 'image/png' || ![result.width, result.height].every(side => Number.isInteger(side) && side >= 1 && side <= 256) ||
            result.data.length > 4 * Math.ceil(128 * 1024 / 3) ||
            !/^iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/.test(result.data)) return null;
        return 'data:image/png;base64,' + result.data;
      } catch (error) {
        signal.throwIfAborted();
        // Keep the fallback for this identity if a new catalog still names the same broken resource.
        if (error instanceof APIError && (error.status === 409 || error.status === 410)) {
          void client.invalidateQueries({queryKey:installedCatalogKey, exact:true});
        }
        return null;
      }
    },
  });
  return ready && hasVariant ? image.data ?? undefined : undefined;
}

// 连接或应用草稿执行前检查同一安装仍在已读完且有效的目录内。
export function installationIsCurrent(client: QueryClient, pkg: PluginInstallRevision) {
  const state = client.getQueryState<CatalogPackage[]>(installedCatalogKey);
  return state?.status === 'success' && state.fetchStatus === 'idle' && !state.isInvalidated
    && state.data?.some(current => sameInstallation(current, pkg));
}

// 先结束当前目录页请求，再失效相关插件查询，避免旧页恢复刚变化的目录。
export async function refreshInstalledCatalog(client: QueryClient) {
  await client.cancelQueries({queryKey: installedCatalogKey, exact: true});
  await client.invalidateQueries({queryKey: ['plugins']});
}

// `sourceToken` 由后端绑定本地 Skill 文件版本；可调用性和不支持能力供界面决定可用入口。
type LocalSkill = {
  name: string; desc: string; level: string; sourceToken: string;
  userInvocable: boolean; modelInvocable?: boolean; unsupported: string[];
};
type LocalSkillList = {skills: LocalSkill[]; sources: {id: string; name: string}[]};

// 本地 Skill 列表只在指定会话和资料资源范围内查询。
export function useLocalSkills(conversationId: string, resourceId: string, enabled: boolean) {
  return useQuery({
    queryKey: ['manual-skills', conversationId, resourceId],
    queryFn: () => api<LocalSkillList>(pluginEndpoint + '/local-skills?' + new URLSearchParams({conversationId, resourceId})),
    enabled,
  });
}

const contributionKinds = new Map([
  ['skill', '技能'], ['mcp', 'MCP'], ['hook', 'Hook'], ['context-store', '会话状态'],
  ['memory-provider', '记忆实现'], ['worker', '委派贡献'], ['runtime-fact', '运行事实'], ['page', '页面声明'],
]);
const packageCategories = [
  ['skill', '技能'], ['mcp', '工具'], ['worker', '委派'],
  ['memory-provider', '记忆'], ['page', '应用'],
] as const;
export const packageKinds = packageCategories.map(([,label])=>label);

// Filters describe where a package is useful. Keep its complete contribution
// labels for inspection and searches by the underlying protocol vocabulary.
export function packageCategoryLabels(pkg: CatalogPackage, hasApplication: boolean) {
  const kinds = new Set(pkg.contributions.map(item=>item.kind));
  if (hasApplication) kinds.add('page');
  return packageCategories.filter(([kind])=>kinds.has(kind)).map(([,label])=>label);
}
// 把后端贡献类型转为界面可展示的条目摘要。
export function contributions(pkg: CatalogPackage) {
  const kinds = new Set(pkg.contributions.map(item => item.kind));
  return [...contributionKinds].filter(([kind]) => kinds.has(kind)).map(([,label]) => label);
}

// 按安装启用状态与贡献可用性给出插件状态。
export function packageStatus(pkg: CatalogPackage) {
  if (pkg.remoteEnabled && pkg.servers.some(s => s.supported && s.transport !== 'stdio' && !s.connected)) return '待连接';
  return pkg.contributions.some(item => item.status === 'enabled') ? '已启用' : '已停用';
}

// 统一展示安装包或本地资料中的 Skill。保留来源键及内容版本供后续预览核对。
export type CatalogSkill = {
  key: string; name: string; description: string; group: '个人' | '项目' | '来自插件';
  source?: string; sourceToken?: string; pkg?: CatalogPackage; version: string;
  manual: boolean; automatic: boolean; unsupported: string[];
};

// 用插件包安装身份构造目录条目，以后读取时仍需核对该安装。
export function packageSkill(pkg: CatalogPackage, skill: PluginSkill, source = pkg.name): CatalogSkill {
  return {
    key: JSON.stringify(['package', pkg.id, skill.path, skill.name]),
    version: JSON.stringify([pkg.revision, pkg.fingerprint, skill.hash]),
    name: skill.name, description: skill.description, group: '来自插件', source, pkg,
    manual: skill.userInvocable !== false, automatic: skill.modelInvocable !== false, unsupported: skill.unsupported ?? [],
  };
}

// 用后端本地资料令牌构造目录条目，读取时再带回原会话和资源。
export function localSkill(skill: LocalSkill, directory?:string): CatalogSkill {
  const group = skill.level === '用户' ? '个人' : '项目';
  return {
    key: JSON.stringify(['local', skill.sourceToken, skill.name]), version: skill.sourceToken,
    name: skill.name, description: skill.desc, sourceToken: skill.sourceToken, source: group === '项目' ? directory : undefined, group,
    manual: skill.userInvocable, automatic: skill.modelInvocable !== false, unsupported: skill.unsupported,
  };
}

export function skillSourceLabel(skill: CatalogSkill, t: UiTranslator) {
  if (skill.pkg) return t(skill.pkg.servers.length ? '插件技能 · {source} · 同包提供 MCP' : '插件技能 · {source}',
    {source:skill.source ?? skill.pkg.name});
  if (skill.group === '个人') return t('个人技能');
  return skill.source ? t('项目技能 · {source}', {source:skill.source}) : t('项目技能');
}

export type SkillDraft = ToolHandoff & {version: string};
// 按来源选择安装包或本地资料入口，读回内容只形成供用户核对的会话草稿。
export function readSkillDraft(skill: CatalogSkill, conversationId: string, resourceId: string) {
  return skill.pkg
    ? api<SkillDraft>(pluginEndpoint + '/' + encodeURIComponent(skill.pkg.id) + '/skill-draft', {
        name: skill.name, expectedRevision: skill.pkg.revision,
      }, true)
    : api<SkillDraft>(pluginEndpoint + '/local-skills', {
        name: skill.name, sourceToken: skill.sourceToken, conversationId, resourceId,
      }, true);
}
