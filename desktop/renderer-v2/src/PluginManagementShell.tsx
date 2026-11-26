// 管理插件目录、安装包和 Skill 详情之间的导航，保存返回位置与焦点。
// 主要引用方包括 `App.tsx`。

import {useLayoutEffect, useRef, useState, type ReactNode} from 'react';
import {useApplicationSettings} from './ApplicationSettings';
import {PackageDisplayIcon} from './ApplicationIcon';
import {CatalogPackageManagement, InstallPackageButton, ReplacePackageDialog, RestoreBundledPackageButton} from './CatalogPackageManagement';
import type {PluginLogin} from './usePluginLogin';
import {CatalogSkillDetail} from './CatalogSkillDetail';
import {contributions, localSkill, packageCategoryLabels, packageKinds, packageSkill, packageStatus, sameInstallation, skillSourceLabel, useInstalledPackages, useLocalSkills, type CatalogPackage, type CatalogSkill} from './extensionCatalog';
import {useUiLanguage} from './UiLanguage';
import {LegacyManagementPane} from './LegacyManagementPane';
import {MemoryProviderControl} from './MemoryProviderControl';
import type {ToolHandoff} from './ToolHandoff';
import {UiIcon} from './UiIcon';
import {ApplicationUnits} from './ApplicationUnits';
import {packageCategoryIcon, type InstalledApplication, type BundledPackageOffer} from './pluginApplications';
import './extension-catalog.css';

type Section = 'packages' | 'skills' | 'memory' | 'applications' | 'tools';
type View = {page: Section} | {page:'package'; id:string; manage?:boolean} | {page:'skill'; key:string; fromPackage?:string};
export type ManagementViewRequest = {serial:number; target:Exclude<View,{page:'skill'}>};
// 一次入栈的管理页位置。
// `top` 是滚动像素，`focus` 是原目录条目的 `data-catalog-key`。
type Position = {view:View; top:number; focus?:string};
const titles: Record<Section,string> = {packages:'插件', skills:'技能', memory:'记忆实现', applications:'应用与工具', tools:'技能导入与外部工具'};
const icons = {packages:'extensions', skills:'model', memory:'memory', applications:'settings'} as const;

// 用稳定目录键标记条目，以便刷新与返回后定位原焦点。
function CatalogRow({name, description, source, group, icon, rowKey, disabled, onClick}: {
  name:string; description:string; source?:string; group?:CatalogSkill['group']; icon:ReactNode; rowKey:string; disabled?:boolean; onClick:()=>void;
}) {
  return <button type="button" className="catalog-row" data-catalog-key={rowKey} data-skill-source={group} disabled={disabled} onClick={onClick}>
    <span className="catalog-icon">{icon}</span><span className="catalog-row-copy"><strong>{name}</strong>
      {description && <span>{description}</span>}{source && <small>{source}</small>}</span><UiIcon name="forward"/>
  </button>;
}
// 从安装清单及贡献状态生成包简介与可操作入口。
function PackageIntro({pkg, app, ready, openSkill}: {pkg:CatalogPackage; app?:InstalledApplication; ready:boolean; openSkill:(skill:CatalogSkill)=>void}) {
  const {t} = useUiLanguage();
  const pages = pkg.contributions.filter(item => item.kind === 'page');
  const facts = pkg.contributions.filter(item => item.kind === 'runtime-fact');
  return <div className="catalog-intro">
    <p className="catalog-description">{app?.description || pkg.description || '此插件尚未提供用途说明。'}</p>
    {!!pages.length && <section><h2>页面声明</h2>{pages.map(page => <p key={page.localId}>
      <strong>{page.pageTitle || page.localId}</strong> · {page.supported ? page.enabled ? '已启用' : '已停用'
        : page.reason === 'page_descriptor_invalid' ? '页面描述无效' : '等待页面描述'}
    </p>)}</section>}
    {!!(pkg.skills.length || pkg.servers.length) && <section>
      <h2>{t('包内技能')} <small>{pkg.skills.length}</small></h2>
      {!!pkg.skills.length && <div className="catalog-grid">
        {pkg.skills.map(entry => {
          const skill = packageSkill(pkg,entry);
          return <CatalogRow key={skill.key} rowKey={skill.key} disabled={!ready} name={skill.name}
            group={skill.group} description={skill.description} source={skillSourceLabel(skill,t)}
            icon={<UiIcon name="model"/>} onClick={() => openSkill(skill)}/>;
        })}
      </div>}
    </section>}
    {!!pkg.servers.length && <section><h2>{t('MCP 服务')}</h2>
      <p className="catalog-note">{t('在「管理」中查看 MCP 工具和连接状态。')}</p>
      {pkg.servers.map(server => {
      const service = app?.connection?.server === server.name ? app.connection : undefined;
      const contribution = pkg.contributions.find(item => item.kind === 'mcp' && item.localId === server.name);
      const status = service?.status || (contribution?.status === 'disabled' ? '已停用'
        : server.supported ? server.transport === 'stdio' ? '本机服务' : server.connected ? '已连接' : '待连接' : server.reason);
      return <p key={server.name}><strong>{service?.name || server.name}</strong> · {server.displayURL || server.transport} · {status}</p>;
    })}</section>}
    {!!pkg.hooks?.length && <section><h2>观察 Hook</h2>{pkg.hooks.map((hook,i) => <p key={i}>{hook.event} · {hook.entry}</p>)}</section>}
    {!!pkg.contextStores?.length && <section><h2>会话状态</h2>{pkg.contextStores.map(store => <p key={store.name}>{store.name} · {store.description}</p>)}</section>}
    {pkg.memoryProvider && <section><h2>记忆实现</h2><p>{String(pkg.memoryProvider.adapter ?? '记忆适配器')} · {pkg.memoryEnabled ? '当前已选用' : '可在管理页选用'}</p></section>}
    {!!pkg.workers?.length && <section><h2>委派贡献</h2>{pkg.workers.map(worker => <p key={worker.name}>{worker.modelRole ? '任务核心' : '子 Agent'} · {worker.description}</p>)}</section>}
    {!!facts.length && <section><h2>运行事实</h2>{facts.map(item => <p key={item.localId}>{item.localId} · {item.enabled ? '已启用' : '已停用'}</p>)}</section>}
  </div>;
}

// 把宿主安装目录、应用页和会话 Skill 资料串成同一管理导航，路径绑定包和资源归属。
export default function PluginManagementShell({visible, request, conversationId, onUseSkill, onWorkroom, catalog, offer, bundleError, onRetryBundle, applications, onOpenApplication, login}: {
  visible:boolean; request?:ManagementViewRequest; conversationId:string;
  onUseSkill:(value:ToolHandoff, owner:string)=>void; onWorkroom:()=>void;
  catalog:ReturnType<typeof useInstalledPackages>; offer?:BundledPackageOffer; bundleError?:string; onRetryBundle:()=>void; applications:InstalledApplication[]; onOpenApplication:(id:string)=>void;
  login:PluginLogin;
}) {
  const {theme, preferences} = useApplicationSettings();
  const {t} = useUiLanguage();
  const [view, setView] = useState<View>(request?.target ?? {page:'packages'});
  const [navOpen, setNavOpen] = useState(false);
  const [toolsVisited, setToolsVisited] = useState(false);
  const [packageSearch, setPackageSearch] = useState('');
  const [skillSearch, setSkillSearch] = useState('');
  const [kind, setKind] = useState('全部');
  const [source, setSource] = useState('全部');
  const [selection, setSelection] = useState<{owner:string; id:string}>();
  const [replacement, setReplacement] = useState<CatalogPackage>();
  const [replacementNotice, setReplacementNotice] = useState('');
  const resourceId = selection?.owner === conversationId ? selection.id : '';
  const skillPage = view.page === 'skills';
  const needLocal = skillPage || (view.page === 'skill' && !view.fromPackage);
  const locals = useLocalSkills(conversationId, resourceId, visible && needLocal);
  const localSources = locals.data?.sources ?? [];
  const directory = (localSources.find(item => item.id === resourceId) ?? (localSources.length === 1 ? localSources[0] : undefined))?.name;
  const packageNames = new Set<string>(), repeatedNames = new Set<string>();
  for (const item of catalog.packages) {
    if (packageNames.has(item.name)) repeatedNames.add(item.name);
    packageNames.add(item.name);
  }
  const appsFor = (item:CatalogPackage) => applications.filter(app => app.packageId === item.id
    && app.pkg.fingerprint === item.fingerprint && app.pkg.revision === item.revision);
  const appFor = (item:CatalogPackage) => {
    const owned = appsFor(item).filter(entry => entry.dataPageRevision === undefined);
    return owned.length === 1 ? owned[0] : undefined;
  };
  const labelFor = (item:CatalogPackage) => appFor(item)?.name || (repeatedNames.has(item.name) ? item.name + ' · v' + item.version + ' · ' + item.fingerprint.slice(0,8) : item.name);
  const iconFor = (item:CatalogPackage) => {
    const app = appFor(item);
    return <PackageDisplayIcon packageId={item.id} installation={item} theme={theme}
      catalogReady={catalog.complete && visible} imageDataUrl={app?.imageDataUrl}
      defaultImageDataUrl={app?.defaultImageDataUrl} kind={app?.icon ?? packageCategoryIcon(item)}/>;
  };
  const skills = [...(locals.error ? [] : locals.data?.skills ?? []).map(item => localSkill(item,directory)),
    ...catalog.packages.flatMap(pkg => pkg.skills.map(item => packageSkill(pkg,item,labelFor(pkg))))];
  const pkg = view.page === 'package' ? catalog.packages.find(item => item.id === view.id) : undefined;
  const app = pkg && appFor(pkg);
  const skill = view.page === 'skill' ? skills.find(item => item.key === view.key) : undefined;
  const scroller = useRef<HTMLDivElement>(null);
  const layout = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  const stack = useRef<Position[]>([]);
  const restore = useRef<Position | undefined>(undefined);
  const handled = useRef(request?.serial);
  const activeView = useRef(view);
  activeView.current = view;

  // 进入子页时保存原页、滚动与焦点。切换根时丢弃旧导航栈。
  function go(next:View, mode:'push'|'root'|'replace' = 'push') {
    if (mode === 'push') {
      const active = document.activeElement as HTMLElement | null;
      stack.current.push({view, top:scroller.current?.scrollTop ?? 0, focus:active?.dataset.catalogKey});
    } else if (mode === 'root') stack.current = [];
    restore.current = undefined;
    if (next.page === 'tools') setToolsVisited(true);
    setNavOpen(false);
    setView(next);
  }
  // 返回原管理页并恢复对应的阅读位置，缺栈时回到页面的归属入口。
  function back() {
    const previous = stack.current.pop();
    restore.current = previous;
    const fallback = view.page === 'skill' && view.fromPackage ? {page:'package' as const,id:view.fromPackage}
      : {page:view.page === 'skill' ? 'skills' as const : view.page === 'tools' ? 'applications' as const : 'packages' as const};
    setView(previous?.view ?? fallback);
    setNavOpen(false);
  }
  useLayoutEffect(() => {
    if (request && request.serial !== handled.current) {
      handled.current = request.serial;
      go(request.target, 'root');
    }
  }, [request]);
  useLayoutEffect(() => {
    if (!visible) { setNavOpen(false); return; }
    const saved = restore.current;
    restore.current = undefined;
    const target = saved?.focus && layout.current?.querySelector<HTMLElement>('[data-catalog-key="' + CSS.escape(saved.focus) + '"]');
    const heading = scroller.current?.querySelector<HTMLElement>('[data-catalog-heading]');
    (target && target.getClientRects().length ? target : heading)?.focus({preventScroll:true});
    if (scroller.current) scroller.current.scrollTop = saved?.top ?? 0;
  }, [view, visible]);
  useLayoutEffect(() => {
    if (!navOpen) return;
    const nav = sidebar.current;
    (nav?.querySelector<HTMLButtonElement>('[aria-current="page"]') ?? nav?.querySelector('button'))?.focus();
    const narrow = window.matchMedia('(max-width:720px)');
    const onResize = () => {if (!narrow.matches) setNavOpen(false);};
    narrow.addEventListener('change', onResize);
    return () => narrow.removeEventListener('change', onResize);
  }, [navOpen]);
  const closeNav = () => {setNavOpen(false); opener.current?.focus();};
  const section = (id:keyof typeof icons) => <button type="button" key={id} aria-current={view.page === id || (id === 'skills' && view.page === 'skill' && !view.fromPackage) || (id === 'applications' && view.page === 'tools') ? 'page' : undefined}
    data-catalog-key={'nav-' + id} onClick={() => go({page:id},'root')}><UiIcon name={icons[id]}/>{titles[id]}</button>;
  const search = skillPage ? skillSearch : packageSearch;
  const filter = skillPage ? source : kind;
  const matches = (...text:string[]) => text.join(' ').toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
  const shownSkills = skills.filter(item => (source === '全部' || item.group === source) &&
    matches(item.name,item.description,skillSourceLabel(item,t)));
  const shownPackages = catalog.packages.map(item=>({
    item, categories:packageCategoryLabels(item, appsFor(item).length>0),
  })).filter(({item,categories})=>(kind==='全部'||categories.some(value=>value===kind)) &&
    matches(labelFor(item),item.name,item.description,...contributions(item),...categories,...categories.map(value=>t(value))));
  const filters = skillPage ? (['全部','个人','项目','来自插件'] as const) : (['全部',...packageKinds] as const);
  const listPage = skillPage || view.page === 'packages';
  const localRelevant = needLocal && (view.page === 'skill' ? !skill?.pkg : source !== '来自插件');
  const packageRelevant = !skillPage || source === '全部' || source === '来自插件';
  const complete = (!packageRelevant || catalog.complete) && (!localRelevant || locals.isSuccess);
  const readStatus = <>
    {packageRelevant && !catalog.complete && (catalog.error
      ? <p role="alert">插件目录读取不完整。{catalog.error} <button onClick={() => void catalog.retry()}>重新读取插件</button></p>
      : <p role="status">正在读取插件目录…</p>)}
    {localRelevant && (locals.error
      ? <p role="alert">{locals.error.message} <button onClick={() => void locals.refetch()}>重新读取技能</button>
        {resourceId && <button onClick={() => {setSelection(undefined);go({page:'skills'},'root');}}>重新选择来源</button>}</p>
      : locals.isPending && <p role="status">正在读取本机技能…</p>)}
  </>;
  function skillRow(item:CatalogSkill) {
    // Keep focus on the effective local row even when a refreshed file gets a new source token.
    const rowKey = item.pkg ? item.key : JSON.stringify([conversationId,resourceId,item.group,item.name]);
    return <CatalogRow key={rowKey} rowKey={rowKey} name={item.name} description={item.description}
      source={skillSourceLabel(item,t)} group={item.group} disabled={!!item.pkg && !catalog.complete} icon={<UiIcon name="model"/>} onClick={() => go({page:'skill',key:item.key})}/>;
  }
  const heading = (label:string) => <h1 tabIndex={-1} data-catalog-heading>{label}</h1>;
  const units=(search='')=><ApplicationUnits applications={applications} theme={theme} search={search} onWorkroom={onWorkroom}
    onOpen={onOpenApplication} onManage={id=>go({page:'package',id,manage:true})}/>;
  const backButton = <button type="button" className="catalog-back" onClick={back}><UiIcon name="back"/>返回{view.page === 'skill' ? view.fromPackage ? '所属插件' : '技能列表' : view.page === 'tools' ? '应用与工具' : ''}</button>;

  return <div className={'extension-catalog theme-' + theme} ref={layout} style={{fontSize:preferences.fontSize}} data-nav-open={navOpen || undefined}
    onKeyDown={e => {
      if (navOpen && e.key === 'Escape') {e.preventDefault();closeNav();}
      else if (e.altKey && e.key === 'ArrowLeft' && stack.current.length && !(e.target as HTMLElement).closest('[role="dialog"], dialog')) {e.preventDefault();back();}
    }}>
    <button type="button" ref={opener} className="catalog-mobile-toggle" aria-controls="catalog-navigation" aria-expanded={navOpen}
      onClick={() => setNavOpen(!navOpen)}><UiIcon name="sidebar"/>目录</button>
    {navOpen && <button className="catalog-scrim" tabIndex={-1} aria-label="关闭目录" onClick={closeNav}/>}
    <nav className="catalog-nav" id="catalog-navigation" aria-label="插件目录导航" ref={sidebar}>
      <h2>自定义</h2><div>{section('packages')}{section('skills')}</div>
      <h3>已安装插件</h3><div className="catalog-shortcuts">
        {catalog.packages.map(item => <button key={item.id} data-catalog-key={'shortcut-' + item.id} disabled={!catalog.complete}
          aria-current={(view.page === 'package' && view.id === item.id) || (view.page === 'skill' && view.fromPackage === item.id) ? 'page' : undefined}
          onClick={() => go({page:'package',id:item.id},view.page === 'package' && view.id === item.id ? 'replace' : 'push')} title={labelFor(item)}>{iconFor(item)}<span>{labelFor(item)}</span></button>)}
        {!catalog.packages.length && <small>{catalog.error ? '目录读取失败' : catalog.complete ? '尚未安装插件' : '正在读取…'}</small>}
      </div><div className="catalog-utilities">{section('memory')}{section('applications')}</div>
    </nav>
    <div className="catalog-content" ref={scroller} inert={navOpen}>
      <div className="catalog-page" hidden={view.page === 'tools'}>
        {listPage && <>
          <header className="catalog-header">{heading(skillPage ? '技能' : '插件')}
            <div className="catalog-actions"><label className="catalog-search"><UiIcon name="search"/><input type="search" aria-label={skillPage ? '搜索技能' : '搜索插件'}
              placeholder={skillPage ? '搜索技能' : '搜索插件'} value={search} onChange={e => (skillPage ? setSkillSearch : setPackageSearch)(e.target.value)}/></label>
              {!skillPage && visible && <InstallPackageButton/>}</div></header>
          <div className="catalog-filters" role="group" aria-label={t(skillPage ? '技能来源' : '筛选插件')}>
            {filters.map(value => <button key={value} type="button" aria-pressed={value === filter}
              onClick={() => (skillPage ? setSource : setKind)(value)}>{t(value)}</button>)}
          </div>
          {!skillPage && visible && (kind==='全部'||kind==='应用') && units(packageSearch)}
          {skillPage && source !== '来自插件' && !!locals.data?.sources.length && <label className="catalog-scope">代码目录
            <select value={resourceId} onChange={e => setSelection({owner:conversationId, id:e.target.value})}>
              <option value="">{locals.data.sources.length === 1 ? '当前项目（自动）' : '仅用户技能'}</option>
              {locals.data.sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select></label>}
          {readStatus}
          {!skillPage && replacementNotice && <p role="status">{replacementNotice}</p>}
          <h2 className="catalog-list-heading">{search.trim() ? (skillPage ? '搜索结果' : '插件包搜索结果') : skillPage ? '技能列表' : '已安装的 Modot 插件包'} {complete && <small>{skillPage ? shownSkills.length : shownPackages.length}</small>}</h2>
          <div className="catalog-grid">{skillPage ? shownSkills.map(skillRow) : shownPackages.map(({item,categories}) => <CatalogRow key={item.id} rowKey={item.id} disabled={!catalog.complete}
            name={labelFor(item)} description={appFor(item)?.description || item.description} source={['v'+item.version,...categories.map(value=>t(value)),packageStatus(item)].join(' · ')}
            icon={iconFor(item)} onClick={() => go({page:'package',id:item.id})}/>)}</div>
          {complete && !(skillPage ? shownSkills : shownPackages).length && <p className="catalog-note">{filter !== '全部' || search.trim() ? (skillPage?'没有匹配项。':'没有匹配的 Modot 插件包。') : skillPage ? '当前范围内没有技能。' : '尚未安装 Modot 插件包，可从本机目录安装。'}
            {(filter !== '全部' || !!search.trim()) && <button className="catalog-link" onClick={() => {if (skillPage) {setSource('全部');setSkillSearch('');} else {setKind('全部');setPackageSearch('');}}}>清除筛选</button>}</p>}
          {!skillPage && bundleError && <p role="alert">随附插件暂不可用。{bundleError} <button onClick={onRetryBundle}>重新核对</button></p>}
          {!skillPage && catalog.complete && offer && (kind==='全部'||kind==='工具'||kind==='应用') && matches(offer.name,offer.packageName,offer.kind) &&
            <section><h2>随 Modot 提供</h2><RestoreBundledPackageButton offer={offer}/></section>}
        </>}
        {(view.page === 'package' || view.page === 'skill') && <>{backButton}{readStatus}</>}
        {view.page === 'package' && pkg && <article className="catalog-detail">
          <header className="catalog-identity"><span className="catalog-icon">{iconFor(pkg)}</span><div>{heading(labelFor(pkg))}<small>{app?.sourceLabel || pkg.format} · v{pkg.version} · {packageStatus(pkg)}</small></div>
            {appsFor(pkg).map(entry => <button key={entry.id} className="catalog-open-app" disabled={!catalog.complete} onClick={()=>onOpenApplication(entry.id)}>打开 {entry.name} <UiIcon name="open"/></button>)}</header>
          <nav className="catalog-segments" aria-label="插件内部页面">{[false,true].map(manage => <button key={String(manage)} aria-pressed={!!view.manage === manage}
            onClick={() => go({...view,manage},'replace')}>{manage ? '管理' : '介绍'}</button>)}</nav>
          {view.manage ? visible && <CatalogPackageManagement key={JSON.stringify([pkg.id, pkg.fingerprint, pkg.revision])}
            pkg={pkg} ready={catalog.complete} login={login} onMemory={() => go({page:'memory'})} onReplace={() => {
              setReplacementNotice('');
              setReplacement(pkg);
            }} onRemoved={() => {
            if (activeView.current.page === 'package' && activeView.current.id === pkg.id) go({page:'packages'},'root');
          }}/> : <PackageIntro pkg={pkg} app={app} ready={catalog.complete} openSkill={item => go({page:'skill',key:item.key,fromPackage:pkg.id})}/>}
        </article>}
        {view.page === 'skill' && visible && skill && <CatalogSkillDetail key={JSON.stringify([conversationId, resourceId, skill.key, skill.version])}
          skill={skill} conversationId={conversationId} resourceId={resourceId} catalogReady={catalog.complete}
          onUse={onUseSkill} onBack={back} onPackage={id => go({page:'package',id})}/>}
        {((view.page === 'package' && !pkg) || (view.page === 'skill' && !skill)) && complete && <p role="status">此项的来源或版本已经变化，请返回目录重新选择。</p>}
        {view.page === 'memory' && <>{stack.current.length > 0 && backButton}<MemoryProviderControl visible={visible} heading={heading(titles.memory)}/></>}
        {view.page === 'applications' && <>{heading(titles.applications)}{readStatus}{visible&&units()}
          <section><h2>导入与连接</h2><button data-catalog-key="open-tools" onClick={() => go({page:'tools'})}>技能导入与外部工具</button></section></>}
        {login.message && <p role="status">{login.message}</p>}
      </div>
      {toolsVisited && <div className="catalog-tools" hidden={view.page !== 'tools'}>{backButton}{heading(titles.tools)}
        <LegacyManagementPane section="skills" embedded open={visible && view.page === 'tools' && !navOpen} theme={theme} onClose={back}/></div>}
    </div>
    {replacement && <ReplacePackageDialog pkg={replacement} visible={visible}
      ready={catalog.complete && catalog.packages.some(item => sameInstallation(item, replacement))}
      onClose={() => setReplacement(undefined)} onReplaced={notice => {
        setReplacement(undefined);
        setReplacementNotice(notice);
        if (activeView.current.page === 'package' && activeView.current.id === replacement.id) go({page:'packages'},'root');
      }}/>}
  </div>;
}
