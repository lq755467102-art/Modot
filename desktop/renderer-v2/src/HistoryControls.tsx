import {useUiLanguage} from "./UiLanguage";
import type {useThreadReader} from "./useThreadReader";

export function HistoryControls({state}:{state:ReturnType<typeof useThreadReader>}) {
  const {t}=useUiLanguage();
  const error=state.readFailure?.message??(state.historyBefore>0?state.past.error?.message:undefined);
  if(!error)return null;
  return <p role="alert">{error} <button type="button" onClick={()=>{
    if(state.readFailure)state.retryRead();else void state.past.refetch();
  }}>{t("重试")}</button></p>;
}
