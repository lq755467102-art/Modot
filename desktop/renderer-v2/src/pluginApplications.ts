// 将安装包、应用贡献和连接记录归集为插件管理与导航所用的应用项。
// 主要引用方包括 `App.tsx` 和 `githubIntegration.ts`。
import type {ComponentProps} from 'react';
import type {ApplicationIconKind} from './ApplicationIcon';
import type {UiIcon} from './UiIcon';
import type {CatalogPackage} from './extensionCatalog';
import type {PluginPackage} from './generated';
import type {ToolHandoff} from './ToolHandoff';

// 内置适配器与纯数据页共用安装身份及应用导航。
export type ApplicationConnection = {server:string; name:string; address?:string; status:string; detail:string};
// 桌面端已登记的页面贡献。
// 页面 ID 在所属插件包内有效，`images` 按主题供显示，`connection` 是可选的连接摘要。
export type ApplicationPage = {
  id:string;
  name:string;
  icon?:ApplicationIconKind;
  images?:{light?:string; dark?:string; default?:string};
  subtitle?:string;
  description:string;
  sourceLabel:string;
  prompts?:readonly (ToolHandoff & {icon?:ComponentProps<typeof UiIcon>['name']})[];
  promptHint?:string;
  connection?:ApplicationConnection;
  dataPageRevision?:number;
};

export type ApplicationRegistration = {
  installation:{id:string; fingerprint:string};
  page:(pkg:PluginPackage) => ApplicationPage;
};
export type InstalledApplication = Omit<ApplicationPage, 'images'> & {
  pkg:CatalogPackage; packageId:string; pageId:string; imageDataUrl?:string; defaultImageDataUrl?:string; available:true;
};
// 宿主可安装的随包插件描述。`fingerprint` 用于安装请求核对原包内容。
export type BundledPackageOffer = {
  id:string; fingerprint:string; path:string; notice?:string; name:string; packageName:string; kind:string;
};

// 未提供页面图标时根据已声明的服务器或 Skill 贡献选择类别。
export function packageCategoryIcon(pkg:PluginPackage):ApplicationIconKind {
  if (pkg.servers.length) return 'tool';
  return pkg.skills.length ? 'document' : 'extension';
}

// 将已登记页面绑定到仍匹配指纹的安装包，并按当前主题选择图片。
export function installedApplications(packages:readonly CatalogPackage[], registrations:readonly ApplicationRegistration[], theme:string) {
  const apps:InstalledApplication[] = [];
  for (const {installation, page} of registrations) {
    const pkg = packages.find(item => !item.removed && item.id === installation.id && item.fingerprint === installation.fingerprint);
    if (!pkg) continue;
    const {images, ...display} = page(pkg);
    // Page IDs are local to an installed package. Navigation must distinguish two packages' "overview".
    const id = JSON.stringify(display.dataPageRevision === undefined
      ? [installation.id, pkg.fingerprint, display.id]
      : ['data-page', installation.id, pkg.fingerprint, pkg.revision, display.id, display.dataPageRevision]);
    apps.push({
      ...display, id, pageId:display.id, icon:display.icon ?? packageCategoryIcon(pkg),
      imageDataUrl:images?.[theme === 'light' ? 'light' : 'dark'], defaultImageDataUrl:images?.default,
      packageId:installation.id, pkg, available:true,
    });
  }
  return apps;
}

// 只有已启用且带宿主修订的目录页才能注册快捷入口。
export function dataPageRegistrations(packages: readonly CatalogPackage[]): ApplicationRegistration[] {
  return packages.flatMap(pkg => pkg.contributions.flatMap(row => {
    const revision = row.pageRevision;
    if (row.kind !== 'page' || !row.supported || !row.enabled || row.status !== 'enabled' ||
        typeof revision !== 'number' || !row.pageTitle) return [];
    const page: ApplicationPage = {
      id:row.localId, name:row.pageTitle, description:'', sourceLabel:pkg.name, icon:'extension',
      dataPageRevision:revision,
    };
    return [{installation:pkg, page:() => page}];
  }));
}

export type CatalogRead = {loaded:number; total:number};
// 以安装版本和服务器标识隔离工具目录的读取进度。
export function catalogReadKey(pkg:PluginPackage, server:string) {
  return ['plugin-tool-reads',pkg.id,pkg.fingerprint,pkg.revision,server] as const;
}
