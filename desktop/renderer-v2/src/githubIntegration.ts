/// <reference types="vite/client" />
// 把已安装的 GitHub MCP 包转换为内置应用页面与连接状态摘要。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `useDesktopApplications.ts`。

import lightMark from './assets/github/github.png?inline';
import darkMark from './assets/github/github-dark.png?inline';
import type {PluginPackage, PluginServer} from './generated';
import type {ApplicationConnection, ApplicationPage} from './pluginApplications';

// 由宿主的服务器摘要呈现账号状态。
// 仅官方 GitHub MCP 能生成此摘要，MCP 开关控制会话工具可用性。
export function githubConnection(pkg:PluginPackage, server?:PluginServer):ApplicationConnection | undefined {
  if (!server?.supported || server.displayURL !== 'https://api.githubcopilot.com/mcp/') return;
  const status = server.githubAccountConnected ? '已登录' : server.connected ? '已保存访问令牌' : '待登录';
  const detail = server.githubAccountConnected ? (pkg.remoteEnabled ? '' : '需要在会话中使用 GitHub 工具时，可在插件管理中启用 MCP。')
    : server.githubAppConfigured ? '连接账号后，可以按需选择仓库。' : '配置 GitHub App 的公开 Client ID 后即可登录。';
  return {server:server.name, name:'GitHub MCP', address:server.displayURL, status, detail};
}

export const githubPackage = {name:'GitHub',packageName:'github-mcp',kind:'MCP'};
const prompts = [
  {title:'了解仓库', icon:'sources', text:'请结合这个仓库的代码和文档，梳理登录与权限校验流程，并标出关键文件。'},
  {title:'审阅拉取请求', icon:'branch', text:'请审阅我指定的拉取请求，概括主要改动、可能的回归和待补的测试。'},
  {title:'整理本周进展', icon:'activity', text:'请根据这个仓库最近 7 天的提交和已合并的拉取请求，整理已交付的工作、风险与下一步。'},
] as const;

// 将安装版插件转换为桌面内置的 GitHub 应用页；主题图与提示模板不随服务器返回内容变化。
export function githubPage(pkg:PluginPackage):ApplicationPage {
  return {
    id:'github', name:githubPackage.name, icon:'code', images:{light:lightMark,dark:darkMark},
    sourceLabel:'Modot 内置 · GitHub MCP', subtitle:'仓库、拉取请求与 Actions 检查',
    description:'在会话里了解仓库、审阅拉取请求，或汇总最近一周的进展。',
    prompts, promptHint:'选择一项加入当前草稿，补充仓库或拉取请求后发送。',
    connection:githubConnection(pkg,pkg.servers.find(server => server.name === 'github')),
  };
}
