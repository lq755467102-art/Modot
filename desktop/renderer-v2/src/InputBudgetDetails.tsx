// Use the same estimates for a prepared request and for a blocked one.
import type { InputBudget } from "./generated";
import { useUiLanguage } from "./UiLanguage";
import { Fragment } from "react";
import type { UiMessage } from "./uiMessages";

const sources: Record<string, UiMessage> = {
  system: "系统指令",
  workspaceInstructions: "项目说明",
  skills: "技能目录",
  plugins: "插件目录",
  pluginFacts: "插件事实",
  baseTools: "基础工具",
  extraTools: "按需工具",
  runtimeUpdate: "运行资料",
  attachments: "附件",
};

export function InputBudgetDetails({budget}: {budget: InputBudget}) {
  const {t}=useUiLanguage();
  const amount=(value: number | null) => value?.toLocaleString() ?? t("未知");
  const deferred=budget.deferred.flatMap(key => sources[key] ? [t(sources[key])] : []);
  return <details className="input-budget-details">
    <summary>{t("查看输入分项")}</summary>
    <p>{t("本地估算")} · {t("固定开销")} {amount(budget.fixed)}
      {" · "}{t("本轮材料")} {amount(budget.current)}
      {" · "}{t("历史记录")} {amount(budget.history)} token</p>
    <p>{t("可用输入")} {amount(budget.inputLimit)}
      {" · "}{t("模型窗口")} {amount(budget.modelWindow)}
      {" · "}{t("输出预留")} {amount(budget.reservedOutput)}
      {budget.userLimit !== null && <>{" · "}{t("会话输入上限")} {amount(budget.userLimit)}</>}
      {!!budget.historyThreshold && <>{" · "}{t("历史压缩触发线")} {amount(budget.historyThreshold)}</>} token</p>
    {budget.providerInput != null && <p>{t("接口报告的输入")} {amount(budget.providerInput)} token</p>}
    {budget.decisionTokens != null && <p>{t(budget.decisionMethod === "unmeasured_input_bound"
      ? "容量校验使用的保守上界" : "容量校验使用的输入")} {amount(budget.decisionTokens)} token</p>}
    {deferred.length > 0 && <p>{t("已改为按需加载")} · {deferred.join("、")}</p>}
    <dl className="context-inspection-identity">
      {Object.entries(budget.sources).filter(([key, value])=>sources[key] && value>0).map(([key, value])=>
        <Fragment key={key}><dt>{t(sources[key])}</dt><dd>{amount(value)} token</dd></Fragment>)}
    </dl>
  </details>;
}
