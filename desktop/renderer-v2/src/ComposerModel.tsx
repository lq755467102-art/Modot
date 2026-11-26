// 让会话输入区展示可用模型与当前选择，并按模型配置修订提交切换。
// 主要引用方包括 `AgentHome.tsx`。
import { useUiLanguage } from "./UiLanguage";
import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

// 后端模型列表的一条显示记录。
// `name` 是档案显示名，`model` 是提供方的模型名，提交选择使用 `id`。
type ModelProfile = { id: string; name: string; model: string };
// 模型接口的配置快照。`revision` 用于防止修改覆盖较新的选择，候选列表由同一快照提供。
type ModelConfiguration = {
  revision: string;
  main: string;
  profiles: ModelProfile[];
  effective?: { main?: { model: string } };
  error?: string;
};

export function useComposerModel() {
  const models = useQuery({ queryKey: ["models"], queryFn: () => api<ModelConfiguration>("/api/models") });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const data = models.data;
  const canSelect = !!data?.revision && !models.error && !data.error && !models.isFetching && !saving && !error;

  async function choose(id: string) {
    if (!canSelect || !data || inFlight.current) return false;
    inFlight.current = true;
    setSaving(true);
    try {
      await api("/api/models", {
        act: "select", role: "main", id, expectedRevision: data.revision,
      });
      await models.refetch({ throwOnError: true });
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "默认模型未保存，请重新读取当前配置。");
      return false;
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  }

  async function refresh() {
    const result = await models.refetch();
    if (!result.error && !result.data?.error) setError("");
  }
  return { models, canSelect, saving, error, choose, refresh };
}

export function ComposerModel({ control, onSettings }: {
  control: ReturnType<typeof useComposerModel>;
  onSettings: () => void;
}) {
  const { t } = useUiLanguage();
  const { models, canSelect, saving, error, choose, refresh } = control;
  const [open, setOpen] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  const data = models.data;
  const currentModel = data?.effective?.main?.model;

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!host.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  async function selectModel(id: string, button: HTMLButtonElement) {
    const openedPanel = button.closest(".composer-model-popover");
    if (id !== data?.main && !(await choose(id))) return;
    if (openedPanel?.isConnected) {
      setOpen(false);
      trigger.current?.focus({ preventScroll: true });
    }
  }

  return <div className="composer-model" ref={host} onBlur={event => {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => {
    if (open && event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      trigger.current?.focus();
    }
  }}>
    <button ref={trigger} id="mset" className="mset" type="button" aria-label={t("选择模型")}
      title={currentModel ? `${t("L0 默认模型")} · ${currentModel}` : t("选择 L0 默认模型")}
      aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
      <b>{currentModel || t("选择模型")}</b><span aria-hidden="true">⌄</span>
    </button>
    {open && <section className="composer-model-popover" id={panelId} aria-label={t("L0 默认模型")}>
      <header><strong>{t("L0 默认模型")}</strong><small>{t("之后所有会话的新运行都用它，不改变已有记录")}</small></header>
      {models.isPending ? <p role="status">{t("正在读取模型配置…")}</p> : <>
        <div className="composer-model-options" role="group" aria-label={t("已保存连接")} aria-busy={saving}>
          <button type="button" aria-pressed={data?.main === ""} disabled={!canSelect} onClick={event => void selectModel("", event.currentTarget)}>
            <strong>{t("环境配置")}</strong>{!data?.main && currentModel && <small>{currentModel}</small>}
          </button>
          {data?.profiles?.map(profile => <button key={profile.id} type="button" data-profile-id={profile.id}
            aria-pressed={data.main === profile.id} disabled={!canSelect} onClick={event => void selectModel(profile.id, event.currentTarget)}>
            <strong>{profile.name}</strong><small>{profile.model}</small>
          </button>)}
        </div>
        {saving && <small role="status">{t("正在保存默认模型…")}</small>}
        {error && <p role="alert">{error}</p>}
        {(models.error || data?.error) && <p role="alert">{t("暂时无法读取当前模型配置。")}</p>}
        {(error || models.error || data?.error) && <button type="button" disabled={models.isFetching}
          onClick={() => void refresh()}>{t("刷新当前选择")}</button>}
      </>}
      <button type="button" className="composer-model-settings" onClick={() => {
        setOpen(false);
        trigger.current?.focus({ preventScroll: true });
        onSettings();
      }}>{t("管理模型")}</button>
    </section>}
  </div>;
}
