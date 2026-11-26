// 组织插件包的安装、替换、启停与连接页面，提交操作时重查安装身份。
// 主要引用方包括 `PluginManagementShell.tsx`。

import {useEffect, useRef, useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {api, APIError} from './client';
import {ExtensionDetails} from './ExtensionCard';
import {PackageIconPicker} from './PackageIconPicker';
import {memoryProviderKey} from './MemoryProviderControl';
import type {InstalledContribution, PluginDataPageState, PluginInstallRevision, PluginPackage, PluginReplacement, PluginServer, PluginToolsPage} from './generated';
import {installationIsCurrent, pluginEndpoint, refreshInstalledCatalog, type CatalogPackage} from './extensionCatalog';
import {githubConnection} from './githubIntegration';
import {GitHubRepositoryAccess} from './GitHubRepositoryAccess';
import {PluginLoginPrompt} from './PluginLoginPrompt';
import {catalogReadKey, type BundledPackageOffer} from './pluginApplications';
import {matchesPage, pageRevision, pageRoute} from './pluginDataPages';
import type {PluginLogin} from './usePluginLogin';

// 为管理按钮维护独立的串行提交与错误状态，安装失效类结果会刷新目录。
function useAction() {
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const lock = useRef(false);
  // 保证同一控制器一次只提交一个操作，权限或安装版本变化时重读目录。
  async function run(operation: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError('');
    try { await operation(); }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      if (reason instanceof APIError && [403,409,410].includes(reason.status)) void refreshInstalledCatalog(client);
    }
    finally { lock.current = false; setBusy(false); }
  }
  return {busy, error, run};
}

// 更新插件贡献目录与依赖安装版本的记忆查询。
export function useCatalogRefresh() {
  const client = useQueryClient();
  return () => Promise.all([
    refreshInstalledCatalog(client),
    client.invalidateQueries({queryKey: memoryProviderKey}),
    client.invalidateQueries({queryKey: ['memory-trees']}),
  ]);
}

// 先预览本地目录中的插件，再按预览指纹确认安装。
export function InstallPackageButton() {
  const [preview, setPreview] = useState<{path: string; pkg: PluginPackage}>();
  const [notice, setNotice] = useState('');
  const {busy, error, run} = useAction();
  const refresh = useCatalogRefresh();
  // 目录来自 Electron 选择器，后端读取后生成安装预览。
  async function choose() {
    setNotice('');
    const path = await window.workbench.chooseProject();
    if (path) setPreview({path, pkg: await api<PluginPackage>(pluginEndpoint + '/preview', {path})});
  }
  // 把预览中的实际路径和包指纹一起提交，安装后各组件仍可独立启用。
  async function install() {
    if (!preview) return;
    await api(pluginEndpoint + '/install', {path: preview.path, fingerprint: preview.pkg.fingerprint});
    setNotice(preview.pkg.name + ' 已安装，各组件可在管理页启用。');
    setPreview(undefined);
    await refresh();
  }
  const pkg = preview?.pkg;
  return <div className="catalog-install">
    <button type="button" className="catalog-primary" disabled={busy} onClick={() => void run(choose)}>安装插件</button>
    {(notice || error) && !pkg && <p role={error ? 'alert' : 'status'}>{error || notice}</p>}
    {pkg && <ExtensionDetails title="插件安装预览" busy={busy} onClose={() => setPreview(undefined)}>
      <h3>{pkg.name} · {pkg.version}</h3><p>{pkg.description}</p>
      <p>{pkg.format} · {pkg.fileCount} 个文件 · {pkg.bytes} 字节</p>
      <p>版本指纹 <code>{pkg.fingerprint}</code></p>
      <p>技能 {pkg.skills.length} · MCP {pkg.servers.length} · Hook {pkg.hooks?.length ?? 0} · 会话状态 {pkg.contextStores?.length ?? 0}{pkg.memoryProvider && ' · 记忆实现'}{!!pkg.workers?.length && ' · 委派贡献 ' + pkg.workers.length}</p>
      {pkg.servers.map(server => <p key={server.name}>{server.name} · {server.displayURL || server.transport} · {server.supported ? '已适配' : server.reason}</p>)}
      {pkg.unsupported.map(reason => <p key={reason}>{reason}</p>)}
      {!!pkg.omitted.length && <p>略过分发无关目录 · {pkg.omitted.join('、')}</p>}
      {error && <p role="alert">{error}</p>}
      <button disabled={busy} onClick={() => void run(install)}>安装（默认停用）</button>
    </ExtensionDetails>}
  </div>;
}

// 随包插件走同一安装流程，来源由宿主提供。
export function RestoreBundledPackageButton({offer}: {offer:BundledPackageOffer}) {
  const {busy,error,run} = useAction();
  const refresh = useCatalogRefresh();
  // 用随包指纹安装，结果包 ID 与当前介绍页一致后才刷新目录。
  async function restore() {
    const pkg = await api<PluginPackage>(pluginEndpoint + '/install', {path:offer.path, fingerprint:offer.fingerprint});
    if (pkg.id !== offer.id) throw Error('安装记录与随附版本不一致，请重新核对。');
    await refresh();
  }
  return <div><button disabled={busy} onClick={()=>void run(restore)}>{busy ? '正在安装…' : '安装内置 ' + offer.name}</button>
    {offer.notice && <p role="status">{offer.notice}</p>}{error && <p role="alert">{error}</p>}</div>;
}

// 替换操作保留目标安装修订和新来源预览，可按原请求 ID 核对不确定的返回。
export function ReplacePackageDialog({pkg, ready, visible, onClose, onReplaced}: {
  pkg: CatalogPackage; ready: boolean; visible: boolean; onClose: () => void; onReplaced: (notice:string) => void;
}) {
  const client = useQueryClient();
  const {busy, error, run} = useAction();
  const refresh = useCatalogRefresh();
  const [preview, setPreview] = useState<{path:string; pkg:PluginPackage; requestId:string}>();
  const [uncertain, setUncertain] = useState(false);

  // 每次选新目录都生成独立预览和更新请求身份。
  async function choose() {
    setPreview(undefined);
    const path = await window.workbench.chooseProject();
    if (!path) return;
    const candidate = await api<PluginPackage>(pluginEndpoint + '/preview', {path});
    setPreview({path, pkg:candidate, requestId:crypto.randomUUID()});
  }

  // 用原安装修订和候选指纹确认替换。结果不明的重试复用原请求号。
  async function replace() {
    if (!preview || (!uncertain && !installationIsCurrent(client, pkg))) return;
    const body = {path:preview.path, fingerprint:preview.pkg.fingerprint, expectedRevision:pkg.revision, requestId:preview.requestId};
    try {
      // A retry keeps the replacement intent while obtaining a new transport receipt.
      const result = await api<PluginReplacement>(pluginEndpoint + '/' + encodeURIComponent(pkg.id) + '/replace', body, true);
      if (result.requestId !== body.requestId || result.previous.id !== pkg.id || result.previous.fingerprint !== pkg.fingerprint
        || result.previous.revision <= pkg.revision || result.installed.id === pkg.id || result.installed.fingerprint !== body.fingerprint) {
        throw new APIError(502, 'protocol', '替换回执与预览不一致。');
      }
      await refresh();
      onReplaced(`已确认将 ${pkg.name} ${pkg.version} 替换为 ${preview.pkg.name} ${preview.pkg.version}。`);
    } catch (reason) {
      if (reason instanceof APIError && (reason.status === 0 || reason.status === 202 || reason.status >= 500)) {
        setUncertain(true);
        void refresh();
        throw Error('替换结果待核对，可以用原请求重试。');
      }
      setUncertain(false);
      if (reason instanceof APIError && ['plugin_changed','plugin_missing','plugin_target_active','plugin_request_conflict'].includes(reason.code)) setPreview(undefined);
      throw reason;
    }
  }

  if (!visible) return null;
  const unchanged = preview?.pkg.fingerprint === pkg.fingerprint;
  return <ExtensionDetails title="替换插件版本" busy={busy} onClose={onClose}>
    <dl className="catalog-facts catalog-replacement">
      <div><dt>当前安装</dt><dd><strong>{pkg.name} · {pkg.version}</strong><br/><code>{pkg.fingerprint}</code>
        {pkg.sourcePath && <p>{pkg.sourcePath}</p>}</dd></div>
      {preview && <div><dt>新版本</dt><dd><strong>{preview.pkg.name} · {preview.pkg.version}</strong><br/>
        <code>{preview.pkg.fingerprint}</code><p>{preview.path}</p></dd></div>}
    </dl>
    <p>替换后新版本的组件默认停用，连接和写权限需要重新授予。历史回执可以继续查阅。</p>
    {unchanged && <p role="status">所选目录与当前版本内容相同。</p>}
    {!ready && !uncertain && <p role="status">正在核对当前安装。版本发生变化时，请关闭详情后重新选择。</p>}
    {error && <p role="alert">{error}</p>}
    <div className="catalog-actions">
      <button disabled={!ready || busy || uncertain} onClick={() => void run(choose)}>选择新版本目录</button>
      {preview && <button className="catalog-primary" disabled={busy || unchanged || (!ready && !uncertain)} onClick={() => void run(replace)}>
        {uncertain ? '用原请求重试' : '确认替换（默认停用）'}
      </button>}
    </div>
  </ExtensionDetails>;
}

// 以选中安装包的版本展示组件和连接管理，目录待核对时禁用修改入口。
export function CatalogPackageManagement({pkg, ready, login, onMemory, onRemoved, onReplace}: {
  pkg: CatalogPackage; ready: boolean; login: PluginLogin; onMemory: () => void; onRemoved: () => void; onReplace: () => void;
}) {
  const [iconOpen, setIconOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [notice, setNotice] = useState('');
  const {busy, error, run} = useAction();
  const refresh = useCatalogRefresh();
  const client = useQueryClient();
  const disabled = busy || !ready;
  // 先验证选中安装仍然有效，再提交组件变更和对应修订。
  async function change(body: Record<string, unknown>) {
    if (!installationIsCurrent(client, pkg)) return;
    setNotice('');
    try {
      const updated = await api<PluginPackage>(pluginEndpoint + '/' + encodeURIComponent(pkg.id), {expectedRevision: pkg.revision, ...body});
      if (updated.removed) onRemoved();
      else setNotice('设置已保存。新启用的组件从下一轮生效。');
    } finally { await refresh(); }
  }
  const pages = pkg.contributions.filter(item => item.kind === 'page');
  async function togglePage(row: InstalledContribution) {
    if (!installationIsCurrent(client, pkg) || row.pageRevision === undefined) return;
    const enabled = !row.enabled;
    setNotice('');
    const receipt = await api<PluginDataPageState>(pageRoute(pkg.id,row.localId), {
      ...pageRevision(pkg,row.pageRevision), enabled,
    });
    if (!matchesPage(receipt,pkg,row.localId,row.pageRevision + 1) || receipt.enabled !== enabled) {
      await refreshInstalledCatalog(client);
      throw Error('页面启停回执待核对，请重新读取插件目录。');
    }
    await refreshInstalledCatalog(client);
    setNotice(enabled ? '页面已启用。' : '页面已停用。');
  }
  const switches = [
    {key:'skillsEnabled', label:'启用技能', available:pkg.skills.length},
    {key:'remoteEnabled', label:'启用 MCP', available:pkg.servers.length, disabled:!pkg.servers.some(s => s.supported)},
    {key:'writeEnabled', label:'允许申请 MCP 写操作', available:pkg.remoteEnabled},
    {key:'hooksEnabled', label:'启用隔离观察 Hook', available:pkg.hooks?.length},
    {key:'contextEnabled', label:'启用会话状态', available:pkg.contextStores?.length},
    {key:'workersEnabled', label:pkg.workers?.some(worker=>worker.modelRole) ? '启用任务核心贡献' : '启用子 Agent 贡献', available:pkg.workers?.length},
  ] as const;
  return <div className="catalog-management">
    {!!pages.length && <section><h2>插件页面</h2>
      {pages.map(row => <div className="catalog-setting" key={row.localId} data-plugin-page-setting={row.localId}>
        <span>{row.pageTitle || row.localId} · {row.supported ? row.enabled ? '已启用' : '已停用'
          : row.reason === 'page_descriptor_invalid' ? '页面描述无效' : '等待页面描述'}</span>
        {row.supported && row.pageRevision !== undefined && <button type="button" disabled={disabled}
          aria-label={(row.enabled ? '停用页面 ' : '启用页面 ') + (row.pageTitle || row.localId)}
          onClick={() => void run(() => togglePage(row))}>{row.enabled ? '停用页面' : '启用页面'}</button>}
      </div>)}
    </section>}
    {switches.some(item => item.available) && <section><h2>组件与权限</h2>
      {switches.filter(item => item.available).map(item => <label className="catalog-setting" key={item.key}>
        <span>{item.label}</span><input type="checkbox" aria-label={item.label + ' ' + pkg.name}
          checked={!!pkg[item.key]} disabled={disabled || ('disabled' in item && item.disabled)}
          onChange={e => {const checked = e.target.checked; void run(() => change({[item.key]: checked, confirm: checked}));}}/>
      </label>)}
      {pkg.remoteEnabled && <small>MCP 写操作逐次请求批准。</small>}
    </section>}
    {pkg.servers.map(server => <ServerManagement key={server.name} pkg={pkg} server={server} login={login} busy={busy} ready={ready} run={run}/>)}
    {pkg.memoryProvider && <section><h2>记忆实现</h2><p>{pkg.memoryEnabled ? '当前已选用' : '可在记忆实现中选用'}</p>
      <button onClick={onMemory}>选择记忆实现</button></section>}
    <section><h2>安装信息</h2><p>{pkg.format} · {pkg.version} · {pkg.compatibility === 'partial' ? '部分组件可用' : '已支持所列组件'}</p>
      {pkg.unsupported.map(reason => <p key={reason}>{reason}</p>)}
      <details><summary>版本指纹与安装记录</summary><code>{pkg.fingerprint}</code>
        {pkg.sourcePath && <p>{pkg.sourcePath}</p>}<p>{pkg.notice}</p></details>
      <div className="catalog-actions"><button disabled={disabled} onClick={() => setIconOpen(true)}>更改显示图标</button>
        <button disabled={disabled} onClick={onReplace}>替换已安装版本</button>
        <button disabled={disabled} onClick={() => setConfirmRemove(true)}>卸载插件</button></div>
    </section>
    {iconOpen && <PackageIconPicker packageId={pkg.id} name={pkg.name} onClose={() => setIconOpen(false)}/>}
    {confirmRemove && <ExtensionDetails title={'卸载 ' + pkg.name} busy={busy} onClose={() => setConfirmRemove(false)}>
      <p>移除当前启用关系，保留来源包和历史版本。</p>
      <button disabled={disabled} onClick={() => void run(() => change({remove: true}))}>确认卸载</button>
      {error && <p role="alert">{error}</p>}
    </ExtensionDetails>}
    {error && !confirmRemove && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </div>;
}

// 用原安装修订绑定服务器、工具目录和仓库授权视图。
// 账号登录交给 `usePluginLogin.ts`，会话工具仍检查 MCP 启用状态。
function ServerManagement({pkg, server, login, busy, ready, run}: {
  pkg: CatalogPackage; server: PluginServer; login: PluginLogin;
  busy: boolean; ready: boolean; run: ReturnType<typeof useAction>['run'];
}) {
  const [tools, setTools] = useState<PluginToolsPage>();
  const [scopes, setScopes] = useState<string[]>();
  const [selected, setSelected] = useState<string[]>([]);
  const [token, setToken] = useState('');
  const [clientId, setClientId] = useState('');
  const refresh = useCatalogRefresh();
  const root = pluginEndpoint + '/' + encodeURIComponent(pkg.id);
  const target = root + '/connections/' + encodeURIComponent(server.name);
  const available = ready && !busy && !!pkg.remoteEnabled && server.supported;
  const remote = server.transport !== 'stdio' && server.supported;
  const github = remote ? githubConnection(pkg, server) : undefined;
  const queryClient = useQueryClient();
  const revision = {expectedRevision: pkg.revision};
  // 将原安装、服务器和当前选中的权限交给 `usePluginLogin.ts`。
  // 成功启动后收起通用权限选择。
  async function connect() {
    if (await login.start(pkg, server.name, github ? 'GitHub' : server.name, selected)) setScopes(undefined);
  }
  // 沿同一工具目录哈希翻页，安装变化时停止使用已读取的工具列表。
  async function inspect(previous?: PluginToolsPage) {
    if (!installationIsCurrent(queryClient, pkg)) return;
    try {
      const page = await api<PluginToolsPage>(root + '/tools', {server:server.name, ...revision, after:previous?.next ?? 0,
        ...(previous && {catalog_hash:previous.catalogHash})});
      if (!installationIsCurrent(queryClient, pkg)) return;
      if (page.pluginId !== pkg.id || page.fingerprint !== pkg.fingerprint || page.revision !== pkg.revision || page.server !== server.name) {
        throw new APIError(409, 'plugin_changed', '工具目录已变化，请重新读取。');
      }
      const catalog = previous ? {...page, tools:[...previous.tools, ...page.tools]} : page;
      setTools(catalog);
      queryClient.setQueryData(catalogReadKey(pkg,server.name),{loaded:catalog.tools.length,total:catalog.total});
    } catch (reason) {
      setTools(undefined);
      queryClient.removeQueries({queryKey:catalogReadKey(pkg,server.name),exact:true});
      throw reason;
    }
  }
  const status = github?.status || (remote ? (server.connected ? '已连接' : '待连接') : '本机服务');
  // 手动令牌由用户输入后提交给宿主连接接口，发送时清理界面输入。
  const tokenForm = <div>
    <label>访问令牌 <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}/></label>
    <button disabled={!available || !token} onClick={() => void run(async () => {
      const value = token; setToken('');
      await api(target + '/token', {...revision, token:value}, true); await refresh();
    })}>保存令牌</button>
  </div>;
  return <section><h2>{github?.name || server.name}</h2><p>{server.displayURL || server.transport} · {server.reason || status}</p>
    {github && <>
      {github.detail && <p>{github.detail}</p>}
      <details>
        <summary>{server.githubAppConfigured ? '管理 GitHub App 配置' : '配置 GitHub App'}</summary>
        <p>在 GitHub App 设置中启用 Device Flow。连接账号后可按需授权仓库。</p>
        <a href="https://github.com/settings/apps/new" target="_blank" rel="noreferrer">注册私有 GitHub App</a>
        <p><label>GitHub App Client ID <input aria-label="GitHub App Client ID" autoComplete="off" spellCheck={false}
          value={clientId} onChange={e => setClientId(e.target.value)}/></label></p>
        {server.connected && <p>变更 GitHub App 配置将断开当前连接。</p>}
        <div className="catalog-actions">
          <button disabled={!ready || busy || login.starting || !!login.pending || !clientId.trim()}
            onClick={() => void run(async () => {
              if (!installationIsCurrent(queryClient, pkg)) return;
              await api(target + '/github-app', {...revision, clientId:clientId.trim()}, true);
              setClientId(''); await refresh();
            })}>保存 GitHub App</button>
          {server.githubAppConfigured && <button disabled={!ready || busy || login.starting || !!login.pending}
            onClick={() => void run(async () => {
              await api(target + '/github-app', {...revision, remove:true}, true); await refresh();
            })}>清除本机 Client ID</button>}
        </div>
      </details>
    </>}
    <div className="catalog-actions"><button disabled={!available || (github && !server.connected)} onClick={() => void run(() => inspect())}>查看工具目录</button>
      {remote && !github && <button disabled={!available || !!login.pending} onClick={() => void run(async () => {
        const result = await api<{scopes:string[]}>(target + '/scopes', revision, true);
        if (!installationIsCurrent(queryClient, pkg)) return;
        setScopes(result.scopes); setSelected([]);
      })}>查看登录范围</button>}
      {github && server.githubAppConfigured && <button disabled={!ready || busy || login.starting || !!login.pending}
        onClick={() => void connect()}>{server.githubAccountConnected ? '重新登录 GitHub' : '连接 GitHub 账号'}</button>}
      {remote && server.connected && <button disabled={busy || !ready} onClick={() => void run(async () => {
        await api(target + '/disconnect', revision); setTools(undefined); await refresh();
      })}>断开连接</button>}</div>
    {scopes && <fieldset><legend>登录权限</legend>
      {scopes.map(scope => <label key={scope}><input type="checkbox" checked={selected.includes(scope)} onChange={e =>
        setSelected(e.target.checked ? [...selected,scope] : selected.filter(item => item !== scope))}/>{scope}</label>)}
      <button disabled={!available || login.starting || !!login.pending || (!!scopes.length && !selected.length)} onClick={() => void run(connect)}>继续登录</button>
    </fieldset>}
    {remote && <details onToggle={e => {if (!e.currentTarget.open) setToken('');}}>
      <summary>用访问令牌连接</summary>
      {github && <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">在 GitHub 创建个人访问令牌</a>}
      {tokenForm}
    </details>}
    {login.pending?.target === target && <PluginLoginPrompt key={login.pending.id} login={login}/>}
    {github && ready && server.githubAccountConnected &&
      <GitHubRepositoryAccess key={pkg.revision} pluginId={pkg.id} server={server.name} revision={pkg.revision}/>}
    {ready && tools && <div aria-label="插件真实工具目录"><h3>工具目录 · {tools.total}</h3><p>{tools.notice}</p>
      {tools.tools.map(tool => <details key={String(tool.spec.name)}><summary>{String(tool.spec.name)} · {tool.callable ? tool.requiresHuman ? '逐次批准' : '只读' : '未开放'}</summary>
        <p>{tool.notice}</p><pre>{JSON.stringify(tool.spec,null,2)}</pre><small>{tool.schemaHash}</small></details>)}
      {tools.hasMore && <button disabled={!available} onClick={() => void run(() => inspect(tools))}>继续读取工具目录</button>}
    </div>}
  </section>;
}
