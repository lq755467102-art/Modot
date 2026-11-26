// 协调启用插件的浏览器登录，按原安装和尝试编号核对状态、取消与目录刷新。
// 主要引用方包括 `App.tsx` 和 `CatalogPackageManagement.tsx`。

import {useEffect, useRef, useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {api} from './client';
import {installationIsCurrent, pluginEndpoint, refreshInstalledCatalog, type CatalogPackage} from './extensionCatalog';
import type {PluginInstallRevision} from './generated';

// `id` 和 `target` 定位宿主原尝试，`installation` 绑定发起时的包身份。
// `userCode` 只在设备授权时提供给页面展示。
type LoginAttempt = {
  target: string; id: string; name: string; installation: PluginInstallRevision;
  url: string; userCode?: string;
};

// 应用壳传入已核对的安装目录。原包已卸载或指纹不匹配时取消尝试，通用连接与设备授权共用此状态。
export function usePluginLogin(ready: boolean, packages: CatalogPackage[]) {
  const client = useQueryClient();
  const [pending, setPending] = useState<LoginAttempt>();
  const [message, setMessage] = useState('');
  const [starting, setStarting] = useState(false);
  const opening = useRef(false);
  const stillInstalled = !!pending && packages.some(pkg => !pkg.removed &&
    pkg.id === pending.installation.id && pkg.fingerprint === pending.installation.fingerprint);

  useEffect(() => {
    if (!pending || !ready) return;
    if (!stillInstalled) {
      void api(pending.target + '/cancel', {id: pending.id}, true).catch(() => {});
      setPending(undefined);
      setMessage('插件安装已变化，请重新连接。');
      return;
    }
    let live = true;
    let timer: number;
    // 按原尝试读宿主状态。等待时续查，收到任何终态后刷新安装目录并退出当前尝试。
    async function poll(login: LoginAttempt) {
      try {
        const result = await api<{state: string; message?: string}>(login.target + '/status', {id: login.id}, true);
        if (!live) return;
        if (result.state === 'waiting') {
          timer = window.setTimeout(() => void poll(login), 600);
          return;
        }
        setMessage(result.message || login.name + (result.state === 'connected' ? ' 已连接。'
          : result.state === 'cancelled' ? ' 登录已取消。'
          : result.state === 'expired' ? ' 授权已过期，可重新登录。' : ' 登录未完成，可重新连接。'));
        void refreshInstalledCatalog(client);
      } catch (reason) {
        if (!live) return;
        setMessage(reason instanceof Error ? reason.message : String(reason));
      }
      setPending(undefined);
    }
    timer = window.setTimeout(() => void poll(pending), 600);
    return () => { live = false; window.clearTimeout(timer); };
  }, [pending, client, ready, stillInstalled]);

  // 先确认目录中的安装修订，再申请后端登录并用 Electron 打开系统浏览器。
  // 打开失败时取消刚建立的尝试。
  async function start(pkg: CatalogPackage, server: string, label: string, scopes: string[] = []) {
    if (pending || opening.current || !installationIsCurrent(client, pkg)) return false;
    const target = pluginEndpoint + '/' + encodeURIComponent(pkg.id) + '/connections/' + encodeURIComponent(server);
    opening.current = true;
    setStarting(true);
    setMessage('');
    let id: string | undefined;
    try {
      const result = await api<{id:string; url:string; userCode?:string}>(target + '/oauth', {
        expectedRevision: pkg.revision, scopes,
      }, true);
      id = result.id;
      if (!installationIsCurrent(client, pkg)) throw Error('插件设置已变化，请重新连接。');
      await window.workbench.openOAuth(result.url);
      setPending({target, id, name: label, installation: pkg, url:result.url, userCode:result.userCode});
      return true;
    } catch (reason) {
      if (id) void api(target + '/cancel', {id}, true).catch(() => {});
      setMessage(reason instanceof Error ? reason.message : String(reason));
      return false;
    } finally {
      opening.current = false;
      setStarting(false);
    }
  }

  // 让宿主取消对应尝试，结果由原状态轮询确认。
  async function cancel() {
    if (!pending) return;
    try { await api(pending.target + '/cancel', {id: pending.id}, true); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
  }

  // 用当前尝试已有的地址重开浏览器，保留原尝试和用户码。
  async function reopen() {
    if (!pending) return;
    try { await window.workbench.openOAuth(pending.url); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : String(reason)); }
  }
  return {pending, message, starting, start, cancel, reopen};
}

export type PluginLogin = ReturnType<typeof usePluginLogin>;
