// 查询当前可用的桌面应用贡献，并整理已安装页面和连接状态。
// 主要引用方包括 `App.tsx`。
import {useQuery} from '@tanstack/react-query';
import {useInstalledPackages} from './extensionCatalog';
import {githubPage, githubPackage} from './githubIntegration';
import {dataPageRegistrations, installedApplications, type BundledPackageOffer} from './pluginApplications';

export function useDesktopApplications(theme:string) {
  const bundle = useQuery({queryKey:['desktop','bundled-github'], queryFn:() => window.workbench.bundledGitHub(), staleTime:Infinity});
  const catalog = useInstalledPackages(!bundle.isPending);
  const installation = bundle.data;
  const registrations = dataPageRegistrations(catalog.packages);
  if (installation) registrations.push({installation,page:githubPage});
  const installed = installedApplications(catalog.packages, registrations, theme);
  const github = installed.find(entry => entry.packageId === installation?.id && entry.pageId === 'github' && entry.dataPageRevision === undefined);
  const apps = catalog.complete ? installed : [];
  // Keep the last confirmed shortcuts visible while their availability is being checked.
  const shortcuts = catalog.complete ? installed : installed.map(entry => ({...entry,available:false,reason:'插件目录待核对'}));
  const offer:BundledPackageOffer | undefined = catalog.complete && installation && !apps.some(app => app.packageId === installation.id)
    ? {...installation,...githubPackage} : undefined;
  return {catalog, apps, shortcuts, github, offer, error:bundle.error?.message, retry:() => {void bundle.refetch();}};
}
