// 显示 GitHub 账号连接和可用仓库，提供管理仓库授权入口。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `PluginApplicationHome.tsx`。
import {useCallback, useEffect, useRef, useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api} from './client';
import {pluginEndpoint} from './extensionCatalog';
import type {GitHubAccount, GitHubRepositoryPage} from './generated';
import './plugin-home.css';

export function GitHubRepositoryAccess({pluginId, server, revision}: {pluginId:string; server:string; revision:number}) {
  const base = `${pluginEndpoint}/${encodeURIComponent(pluginId)}/connections/${encodeURIComponent(server)}`;
  const [accountsPage,setAccountsPage] = useState(1);
  const [repositoriesPage,setRepositoriesPage] = useState(1);
  const [ownerId,setOwnerId] = useState<number>();
  const [generation,setGeneration] = useState(0);
  const [openError,setOpenError] = useState('');
  const waitingForBrowser = useRef(false);
  const queryKey = ['plugins','github-access',pluginId,server,revision,generation];
  const account = useQuery({queryKey:[...queryKey,'account',accountsPage], retry:false, refetchOnWindowFocus:false,
    gcTime:0, queryFn: () => api<GitHubAccount>(`${base}/github-account?revision=${revision}&page=${accountsPage}`)});
  const accountData = !account.error && !account.isFetching ? account.data : undefined;
  const installations = accountData?.installations ?? [];
  const installation = installations.find(item => item.id===ownerId)
    ?? installations.find(item => item.owner===accountData?.login) ?? installations[0];
  const repositories = useQuery({queryKey:[...queryKey,'repositories',installation?.id,repositoriesPage],
    enabled:!!installation, retry:false, refetchOnWindowFocus:false, gcTime:0,
    queryFn: () => api<GitHubRepositoryPage>(`${base}/github-repositories?revision=${revision}&installationId=${installation!.id}&page=${repositoriesPage}`)});
  const repoData = !repositories.error && !repositories.isFetching ? repositories.data : undefined;
  const manageUrl = installation?.manageUrl;
  const githubUrl = manageUrl ?? accountData?.installUrl;

  const refresh = useCallback(() => {
    waitingForBrowser.current = false;
    setOpenError('');
    setOwnerId(undefined);
    setAccountsPage(1);
    setRepositoriesPage(1);
    setGeneration(value => value+1);
  }, []);

  useEffect(() => {
    function onReturn() { if (waitingForBrowser.current) refresh(); }
    window.addEventListener('focus',onReturn);
    return () => window.removeEventListener('focus',onReturn);
  }, [refresh]);

  function changeAccountsPage(page:number) {
    setOwnerId(undefined);
    setRepositoriesPage(1);
    setAccountsPage(page);
  }

  async function openRepositories() {
    if (!githubUrl) return;
    setOpenError('');
    try {
      await window.workbench.openOAuth(githubUrl);
      waitingForBrowser.current = true;
    } catch (error) {
      setOpenError(error instanceof Error ? error.message : String(error));
    }
  }

  return <section className="github-repository-access" aria-label="GitHub 账号和仓库">
    {!!accountData && <p>GitHub 账号 <strong>@{accountData.login}</strong></p>}
    {account.isFetching && <p role="status">正在核对 GitHub 账号和授权…</p>}
    {account.error && <p role="alert">{account.error.message}</p>}
    <div className="github-repository-actions">
      {githubUrl && <button type="button" onClick={() => void openRepositories()}>{manageUrl ? '管理仓库' : '添加仓库'}</button>}
      <button type="button" onClick={refresh} disabled={account.isFetching || repositories.isFetching}>刷新已授权仓库</button>
    </div>
    {openError && <p role="alert">{openError}</p>}
    {accountData && !githubUrl && <p>在 GitHub 安装已配置的 App 后，刷新已授权仓库。</p>}
    {installations.length>1 && <label>授权账户 <select value={installation?.id} onChange={event => {
      setOwnerId(Number(event.currentTarget.value)); setRepositoriesPage(1);
    }}>{installations.map(item => <option key={item.id} value={item.id}>{item.owner}</option>)}</select></label>}
    {(accountsPage>1 || accountData?.nextPage) && <div className="github-repository-actions">
      {accountsPage>1 && <button type="button" onClick={() => changeAccountsPage(accountsPage-1)}>上一批授权账户</button>}
      {accountData?.nextPage && <button type="button" onClick={() => changeAccountsPage(accountData.nextPage!)}>下一批授权账户</button>}
    </div>}
    {installation && repositories.isFetching && <p role="status">正在读取已授权仓库…</p>}
    {installation && repositories.error && <p role="alert">{repositories.error.message}</p>}
    {((accountData && !installation) || (repoData && !repoData.repositories.length)) && <p>尚未授权仓库。</p>}
    {!!repoData?.repositories.length && <ul aria-label="已授权仓库">
      {repoData.repositories.map(repo => <li key={repo.fullName}>{repo.fullName} <small>{repo.private ? '私有' : '公开'}</small></li>)}
    </ul>}
    {installation && (repositoriesPage>1 || repoData?.nextPage) && <div className="github-repository-actions">
      {repositoriesPage>1 && <button type="button" onClick={() => setRepositoriesPage(repositoriesPage-1)}>上一页仓库</button>}
      {repoData?.nextPage && <button type="button" onClick={() => setRepositoriesPage(repoData.nextPage!)}>下一页仓库</button>}
    </div>}
  </section>;
}
