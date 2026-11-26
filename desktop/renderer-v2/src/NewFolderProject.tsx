// 从会话入口新建项目、打开现有检出或导入公开仓库。
import { useUiLanguage } from "./UiLanguage";
import {useEffect, useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {Dialog} from './Dialog';
import {api} from './client';
import type {Project, RepositoryImport} from './generated';
import {validateReply} from './validate';
import {chooseWorkFolder, workFolderQuery} from './workFolders';
import './work-folders.css';

const importRoute = '/api/v2/projects/imports';

export function NewFolderProject({onCreate, onImported, onClose}: {
  onCreate: (name?: string) => Promise<boolean>;
  onImported: (project: Project) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useUiLanguage();
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [remote, setRemote] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState<RepositoryImport>();
  const root = useQuery(workFolderQuery);
  const active = job?.state === 'running' || job?.state === 'stopping';
  const dismiss = () => { if (!busy && !active) onClose(); };
  const folderName = name.trim() || url.trim().split('/').at(-1)?.replace(/\.git$/, '');
  const destination = root.data?.projects && folderName ? root.data.projects + '\\' + folderName : root.data?.projects;

  useEffect(() => {
    if (!active || !job) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api<RepositoryImport>(importRoute + '/' + job.id);
        if (disposed) return;
        if (next.state === 'completed' && next.project) {
          setBusy(true);
          try {
            await onImported(next.project);
            if (!disposed) onClose();
          } catch (err) {
            if (!disposed) setError((err as Error).message);
          } finally {
            if (!disposed) {
              setBusy(false);
              setJob(next);
            }
          }
          return;
        }
        if (next.state !== 'running' && next.state !== 'stopping') {
          setJob(next);
          setError(next.error || t("未取得导入结果，请核对项目列表。"));
          return;
        }
      } catch (err) {
        if (!disposed) setError((err as Error).message);
      }
      if (!disposed) timer = setTimeout(poll, 500);
    };
    timer = setTimeout(poll, 250);
    return () => { disposed = true; clearTimeout(timer); };
  }, [active, job]);

  async function chooseRoot() {
    setBusy(true);
    setError('');
    try {
      if (await chooseWorkFolder()) await root.refetch();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function submit(existing = false) {
    if (busy || active) return;
    setBusy(true);
    setError('');
    try {
      if (remote && !existing) {
        if (!root.data?.selectionRevision) {
          setError(t("请选择工作总目录。"));
          return;
        }
        setJob(await api<RepositoryImport>(importRoute, {
          url: url.trim(),
          ...(name.trim() && {name: name.trim()}),
          confirm: true,
          workFolderRevision: root.data.selectionRevision,
        }, false, undefined, body => validateReply(importRoute, 'POST', body)));
      } else if (await onCreate(existing ? undefined : name.trim())) {
        onClose();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!job || job.state !== 'running') return;
    setBusy(true);
    try {
      const next = await api<RepositoryImport>(importRoute + '/' + job.id + '/cancel', {});
      if (next.state === 'stopping') setJob(next);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <div className="new-project-backdrop" onClick={dismiss}><Dialog label={t("新建项目")} className="new-project-dialog" onClose={dismiss}>
    <header>
      <h2>{t(remote ? "导入公开仓库" : "新建项目")}</h2>
      <button aria-label={t("关闭新建项目")} disabled={busy || active} onClick={onClose}>×</button>
    </header>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      {remote && <label>{t("公开 HTTPS 仓库地址")}
        <input autoFocus type="url" value={url} disabled={busy || active}
          onChange={event => setUrl(event.target.value)} placeholder="https://github.com/owner/repo.git" />
      </label>}
      <label>{t(remote ? "项目文件夹名称（可选）" : "项目名称")}
        <input autoFocus={!remote} aria-label={t(remote ? "项目文件夹名称" : "项目名称")} maxLength={80}
          value={name} disabled={busy || active} onChange={event => setName(event.target.value)}
          placeholder={t(remote ? "默认使用仓库名称" : "例如：我的网站")} />
      </label>
      <p className="work-folder-location" title={remote ? destination : root.data?.projects}>
        {(remote ? destination : root.data?.projects) || t("正在读取保存位置…")}
      </p>
      {remote && !root.data?.selectionRevision && <button type="button" disabled={busy} onClick={() => void chooseRoot()}>
        {t("选择工作总目录")}
      </button>}
      {active && <p role="status">{t(job?.state === "stopping" ? "正在停止导入…" : "正在下载并核对仓库…")}</p>}
      {(error || root.error) && <p role="alert">{error || (root.error as Error)?.message}</p>}
      <footer>
        {remote ? <>
          <button type="button" disabled={busy || active} onClick={() => { setRemote(false); setError(''); }}>{t("返回")}</button>
          {active
            ? <button type="button" disabled={busy || job?.state === 'stopping'} onClick={() => void cancel()}>{t("取消导入")}</button>
            : <button type="submit" disabled={busy || !url.trim() || !root.data?.selectionRevision}>{t("确认导入")}</button>}
        </> : <>
          <button type="button" disabled={busy} onClick={() => void submit(true)}>{t("打开已有文件夹")}</button>
          <button type="button" disabled={busy} onClick={() => { setRemote(true); setError(''); }}>{t("导入公开仓库")}</button>
          <button type="submit" disabled={busy || !name.trim() || !root.data || !!root.error}>{t(busy ? "正在创建…" : "创建项目")}</button>
        </>}
      </footer>
    </form>
  </Dialog></div>;
}

export function WorkFolderSettings() {
  const { t } = useUiLanguage();
  const data = useQuery(workFolderQuery);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function change() {
    setBusy(true);
    setError('');
    try {
      if (await chooseWorkFolder()) await data.refetch();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return <><div className="settings-line"><strong>{t("工作总目录")}</strong><button disabled={busy} onClick={() => void change()}>{t("更改")}</button></div>
    <p className="work-folder-location">{data.data?.path || t("正在读取…")}</p>
    <p>{t("新项目和独立会话的工作文件放在这里。更改只影响新建内容。")}</p>
    {(error || data.error) && <p role="alert">{error || (data.error as Error)?.message}</p>}
  </>;
}
