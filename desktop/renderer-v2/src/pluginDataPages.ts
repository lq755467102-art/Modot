// 封装数据页详情接口和页面版本匹配，供目录预览与实际应用共用。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `DataPluginApplication.tsx`。
import {api, APIError, queryString} from "./client";
import {pluginEndpoint, sameInstallation, type CatalogPackage} from "./extensionCatalog";
import type {PluginDataPageView} from "./generated";

type PageIdentity = Omit<PluginDataPageView, "page">;

export function pageRoute(packageId: string, pageId: string) {
  return pluginEndpoint + "/contributions/" + queryString(packageId) + "/pages/" + queryString(pageId);
}

export function pageRevision(pkg: CatalogPackage, revision: number) {
  return {fingerprint:pkg.fingerprint, revision:pkg.revision, pageRevision:revision};
}

export function matchesPage(reply: PageIdentity, pkg: CatalogPackage, pageId: string, revision: number) {
  return sameInstallation(reply, pkg) && reply.pageId === pageId && reply.pageRevision === revision;
}

export function stalePage(error: unknown) {
  return error instanceof APIError &&
    ["plugin_changed", "plugin_missing", "plugin_page_changed", "plugin_page_disabled"].includes(error.code);
}

export async function readPluginPage(pkg: CatalogPackage, pageId: string, revision: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    fingerprint:pkg.fingerprint, revision:String(pkg.revision), pageRevision:String(revision),
  });
  const result = await api<PluginDataPageView>(pageRoute(pkg.id, pageId) + "?" + params);
  signal?.throwIfAborted();
  if (!matchesPage(result, pkg, pageId, revision) || result.page.pageId !== pageId ||
      result.page.packageKey !== pkg.packageKey) {
    throw new APIError(409, "plugin_page_changed", "页面已变更，请返回插件管理重新打开。");
  }
  return result.page;
}
