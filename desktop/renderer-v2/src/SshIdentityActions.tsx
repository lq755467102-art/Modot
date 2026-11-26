// 通过宿主管理私库导入的 SSH 身份，界面使用身份状态和公钥。
// 主要引用方包括 `RepositoryImportHelp.tsx`。
import {useState} from "react";
import {api} from "./client";
import {useUiLanguage} from "./UiLanguage";

type PublicIdentity = {id: string; label: string; fingerprint: string; publicKey?: string};
export type SshImportIssue = {runId: string; actionId: string; code: string; url: string; name: string};
export const identityRoute = (id: string) => "/api/v2/projects/imports/identities/" + encodeURIComponent(id);

export function SshIdentityActions({sessionId, actionId, disabled}: {sessionId: string; actionId: string; disabled: boolean}) {
  const {t} = useUiLanguage();
  const [choices, setChoices] = useState<PublicIdentity[]>();
  const [picked, setPicked] = useState<PublicIdentity>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  async function operate(operation: "list" | "select" | "create", id?: string) {
    setBusy(true);
    setError("");
    try {
      const data = await api<{identities?: PublicIdentity[]; identity?: PublicIdentity}>(identityRoute(sessionId),
        {operation, actionId, ...(operation === "list" ? {} : {confirm: true}), ...(id ? {identityId: id} : {})});
      if (data.identities) setChoices(data.identities);
      if (data.identity) {setPicked(data.identity); setChoices(undefined); setCopied(false);}
    } catch (e) {setError(e instanceof Error ? e.message : t("无法读取 SSH 身份"));}
    finally {setBusy(false);}
  }
  async function copy() {
    if (!picked?.publicKey) return;
    try {await navigator.clipboard.writeText(picked.publicKey); setCopied(true);}
    catch {setError(t("请选中下方公钥复制。"));}
  }
  return <div aria-label={t("本机 SSH 身份")} className="repository-ssh-identity">
    <div className="repository-import-actions">
      <button type="button" disabled={disabled || busy} onClick={() => void operate("list")}>{t("选择已有 SSH 身份")}</button>
      <button type="button" disabled={disabled || busy} onClick={() => void operate("create")}>{t("在本机创建 SSH 密钥")}</button>
    </div>
    {choices && (choices.length ? <ul>{choices.map(choice => <li key={choice.id}>
      <button type="button" disabled={disabled || busy} onClick={() => void operate("select", choice.id)}>
        {choice.label} · {choice.fingerprint}</button></li>)}</ul> : <small>{t("本机 SSH 目录中没有可选公钥。")}</small>)}
    {picked && <div><small>{t("本次仓库使用的身份")} · {picked.fingerprint}</small>
      <code style={{overflowWrap:"anywhere",userSelect:"all"}}>{picked.publicKey}</code>
      <div className="repository-import-actions"><button type="button" onClick={() => void copy()}>
        {t(copied ? "已复制" : "复制公钥")}</button></div>
      <small>{t("在仓库托管平台登记这段公钥后，可在本会话重试。")}</small></div>}
    {error && <small role="alert">{error}</small>}
  </div>;
}
