// 展示已登记插件应用的页面、连接状态与会话草稿入口。
// 主要引用方包括 `App.tsx`。

import {useEffect, useId, useRef} from 'react';
import {ApplicationIcon, PackageDisplayIcon} from './ApplicationIcon';
import {UiIcon} from './UiIcon';
import type {CatalogRead, InstalledApplication} from './pluginApplications';
import type {ToolHandoff} from './ToolHandoff';
import type {PluginLogin} from './usePluginLogin';
import {PluginLoginPrompt} from './PluginLoginPrompt';
import {GitHubRepositoryAccess} from './GitHubRepositoryAccess';
import './plugin-home.css';

// `App.tsx` 提供已核对的应用和共享登录状态。
// 账号已登录时由 `GitHubRepositoryAccess.tsx` 按当前安装修订读取仓库授权。
export default function PluginApplicationHome({app, fontSize, theme='default', catalogRead, onManage, onConnect, login, onDraft, draftDisabled, draftNotice, draftError}: {
  app:InstalledApplication; fontSize:number; theme?:string; catalogRead?:CatalogRead;
  onManage?:() => void; onDraft?:(draft:ToolHandoff, index:number) => void;
  draftDisabled?:boolean; draftNotice?:string; draftError?:string;
  onConnect?:()=>void; login:PluginLogin;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const titleId = useId();
  const tasksId = useId();
  const serviceId = useId();
  const service = app.connection;
  // 设备授权的账号状态与已保存的令牌是两项不同记录；账号状态决定是否打开仓库管理视图。
  const connectedAccount = app.pkg.servers.find(server => server.name===service?.server)?.githubAccountConnected;
  const revision = app.pkg.revision;
  useEffect(() => {heading.current?.focus({preventScroll:true});}, [app.id]);
  return <main className="plugin-home" aria-labelledby={titleId} data-application-id={app.id} style={{fontSize}}>
    <div className="plugin-home-page">
      <header className="plugin-home-heading">
        <span className="plugin-home-brand">{onDraft && app.dataPageRevision === undefined
          ? <PackageDisplayIcon packageId={app.packageId} installation={app.pkg} theme={theme} kind={app.icon}
              imageDataUrl={app.imageDataUrl} defaultImageDataUrl={app.defaultImageDataUrl}/>
          : <ApplicationIcon kind={app.icon}/>}</span>
        <div><h1 id={titleId} ref={heading} tabIndex={-1}>{app.name}</h1><p>{app.subtitle || app.description}</p></div>
        {onManage && <button className="plugin-home-manage" onClick={onManage}><UiIcon name="gear"/>管理插件</button>}
      </header>
      {!!app.prompts?.length && <section className="plugin-home-tasks" aria-labelledby={tasksId}>
        <h2 id={tasksId}>{onDraft ? '从会话开始' : '使用示例'}</h2>
        {(draftNotice || app.promptHint) && <p role={draftNotice ? 'status' : undefined}>{draftNotice || app.promptHint}</p>}
        {draftError && <p role="alert">{draftError}</p>}
        {onDraft ? <div className="plugin-home-prompt-list">{app.prompts.map((prompt,index) => <button key={index}
          type="button" disabled={draftDisabled}
          aria-label={'将「' + prompt.title + '」加入草稿'} onClick={() => onDraft({title:prompt.title,text:prompt.text},index)}>
          {prompt.icon && <UiIcon name={prompt.icon}/>}<span><strong>{prompt.title}</strong><small>{prompt.text}</small></span><UiIcon name="forward"/>
        </button>)}</div> : <ul className="plugin-home-prompt-list plugin-home-example-list">
          {app.prompts.map((prompt, index) => <li key={index}><span><strong>{prompt.title}</strong><small>{prompt.text}</small></span></li>)}
        </ul>}
      </section>}
      {service && <section className="plugin-home-service" aria-labelledby={serviceId}>
        <h2 id={serviceId}>GitHub 连接</h2>
        <div><span className="plugin-home-service-icon"><UiIcon name="sources"/></span>
          <div><strong>{service.name}</strong>{service.address && <small>{service.address}</small>}</div>
          <span className="plugin-home-service-state">{service.status}</span>
        </div>
        {service.detail && <p>{service.detail}</p>}
        {onConnect ? <button disabled={login.starting || !!login.pending} onClick={onConnect}>
          {connectedAccount ? '重新登录 GitHub' : '连接 GitHub 账号'}
        </button> : !connectedAccount && <button onClick={onManage}>配置 GitHub 连接</button>}
        {login.pending?.installation.id === app.packageId && <PluginLoginPrompt key={login.pending.id} login={login}/>}
        {login.message && <p role="status">{login.message}</p>}
        {connectedAccount && typeof revision==='number' && <GitHubRepositoryAccess key={revision} pluginId={app.packageId}
          server={service.server} revision={revision}/>}
        {catalogRead && <p>上次读取工具目录 · {catalogRead.loaded} / {catalogRead.total} 项</p>}
      </section>}
    </div>
  </main>;
}
