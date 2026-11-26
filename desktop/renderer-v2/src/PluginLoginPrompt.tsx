// 呈现 GitHub 设备授权码和浏览器入口，登录状态由调用方的连接流程维护。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `PluginApplicationHome.tsx`。
import {useState} from 'react';
import type {PluginLogin} from './usePluginLogin';

export function PluginLoginPrompt({login}: {login: PluginLogin}) {
  const [copyStatus, setCopyStatus] = useState('');
  const code = login.pending?.userCode;

  async function copy() {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopyStatus('授权码已复制。');
    } catch {
      setCopyStatus('可以选中授权码手动复制。');
    }
  }

  return <div className="plugin-login-prompt">
    {code ? <>
      <p role="status">在 GitHub 网页输入这组授权码并确认。</p>
      <span className="plugin-login-actions">
        <code aria-label="GitHub 设备授权码">{code}</code>
        <button type="button" onClick={() => void copy()}>复制授权码</button>
        <button type="button" onClick={() => void login.reopen()}>打开 GitHub 网页</button>
      </span>
      {copyStatus && <p role="status">{copyStatus}</p>}
    </> : <p role="status">正在等待浏览器完成登录…</p>}
    <button type="button" onClick={() => void login.cancel()}>取消登录</button>
  </div>;
}
