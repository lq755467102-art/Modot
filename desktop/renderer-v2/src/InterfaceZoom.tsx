import {useMutation,useQuery,useQueryClient} from "@tanstack/react-query";
import {useUiLanguage} from "./UiLanguage";

export type InterfaceZoomSetting = {
  percent: number;
  percentages: number[];
  defaultPercent: number;
};

const queryKey = ["interface-zoom"] as const;

// Both controls use the same native setting and the same pending update.
export function useInterfaceZoom() {
  const client = useQueryClient();
  const current = useQuery({
    queryKey,
    queryFn: () => window.workbench.interfaceZoom(),
    staleTime: Infinity,
  });
  const save = useMutation({
    mutationFn: (percent: number) => window.workbench.interfaceZoom(percent),
    onSuccess: setting => client.setQueryData(queryKey, setting),
  });
  return {
    setting: current.data,
    pending: save.isPending ? save.variables : undefined,
    error: current.isError ? "无法读取界面缩放。" as const : save.isError ? "保存界面缩放失败，请重试。" as const : undefined,
    change: save.mutate,
    retry: () => void current.refetch(),
  };
}

export type InterfaceZoom = ReturnType<typeof useInterfaceZoom>;

export function InterfaceZoomControl({zoom,className,settingItem,highlighted}:{
  zoom: InterfaceZoom;
  className: string;
  settingItem?: string;
  highlighted?: boolean;
}) {
  const {t} = useUiLanguage();
  const {setting,pending,error} = zoom;
  return <>
    <label className={className} data-setting-item={settingItem} data-highlight={highlighted}>
      {t("界面缩放")}
      <select aria-label={t("界面缩放")} value={pending ?? setting?.percent ?? ""} disabled={!setting || pending !== undefined}
        onChange={event => zoom.change(Number(event.currentTarget.value))}>
        {setting ? setting.percentages.map(percent => <option key={percent} value={percent}>{percent}%</option>)
          : <option value="">{t(error ? "无法读取界面缩放。" : "读取中")}</option>}
      </select>
    </label>
    {error && <small className="application-menu-status" role="alert">{t(error)}{" "}
      {!setting && <button type="button" onClick={zoom.retry}>{t("重试")}</button>}
    </small>}
  </>;
}
