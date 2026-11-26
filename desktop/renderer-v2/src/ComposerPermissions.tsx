// 把会话执行模式映射到可读标签，提供切换与权限状态入口。
// 主要引用方包括 `AgentHome.tsx`、`NewConversationSetup.tsx` 和 `ScheduledTasks.tsx`。
import { useEffect, useRef, useState } from "react";
import { useUiLanguage } from "./UiLanguage";
import { UiIcon } from "./UiIcon";

export const permissionOptions = [
  ["ask", "请求批准", "编辑外部文件和使用互联网时始终询问", "hand"],
  ["auto", "帮我批准", "仅对检测到的风险操作请求批准", "terminal"],
  ["full", "完全访问权限", "可不受限制地访问互联网和你电脑上的任何文件", "shield"],
] as const;
export type PermissionMode = typeof permissionOptions[number][0];

type Props = {
  policy?: string;
  readOnly?: boolean;
  pending: boolean;
  count: number;
  disabled?: boolean;
  onChange: (value: PermissionMode) => Promise<void>;
  onRetry: () => void;
  onSettings: () => void;
};

export function ComposerPermissions({policy, readOnly, pending, count, disabled, onChange, onRetry, onSettings}: Props) {
  const { t } = useUiLanguage();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const choice = permissionOptions.find(([id]) => id === policy);
  const label = t(choice ? (readOnly ? "只读" : choice[1]) : pending ? "正在读取权限…" : "权限读取失败 · 重试");
  function close() {
    setOpen(false);
    trigger.current?.focus();
  }
  useEffect(() => {
    if (!open || !choice) return;
    (menu.current?.querySelector<HTMLButtonElement>('[aria-checked=true]') ?? menu.current?.querySelector('button'))?.focus();
    const outside = (e: PointerEvent) => { if (!host.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); close(); } };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open, choice]);
  async function choose(value: PermissionMode) {
    close();
    if (busy || (value === policy && !readOnly)) return;
    setBusy(true);
    try { await onChange(value); } finally { setBusy(false); }
  }
  return <div className="composer-permissions" ref={host} onBlur={e => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
  }}>
    <button ref={trigger} type="button" id="cgatesel" data-policy={choice?.[0]}
      aria-label={choice ? t("权限") : label} aria-haspopup={choice ? "menu" : undefined}
      aria-expanded={choice ? open : undefined} title={t("权限") + " · " + label}
      disabled={disabled || busy || (!choice && pending)} onClick={() => choice ? setOpen(!open) : onRetry()}
      onKeyDown={e => { if (choice && e.key === "ArrowDown") { e.preventDefault(); setOpen(true); } }}>
      <UiIcon name={choice?.[3] ?? "shield"}/><span>{label}</span>
      {count > 0 && <span aria-label={t("{count}项待批准", {count})}> · {count}</span>}
      {choice && <span aria-hidden="true">⌄</span>}
    </button>
    {open && choice && <div ref={menu} className="composer-permissions-menu" role="menu" aria-label={t("权限选项")}
      onKeyDown={e => {
        if (!["ArrowDown","ArrowUp","Home","End"].includes(e.key)) return;
        e.preventDefault();
        const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button"));
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 :
          (index + (e.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
        buttons[next]?.focus();
      }}>
      {permissionOptions.map(([value, name, description, icon]) =>
        <button type="button" key={value} data-mode={value} aria-label={t(name)} role="menuitemradio"
          aria-checked={!readOnly && policy === value} onClick={() => void choose(value)}>
          <UiIcon name={icon}/><span className="permission-copy"><span>{t(name)}</span><small>{t(description)}</small></span>
          {policy === value && !readOnly && <span aria-hidden="true">✓</span>}
        </button>)}
      {count > 0 && <button type="button" role="menuitem" onClick={() => { close(); onSettings(); }}>
        {t("权限与批准记录")}（{count}）
      </button>}
    </div>}
  </div>;
}
