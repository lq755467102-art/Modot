# 委派样例与视觉核心预设

规划者和探路者是可独立安装的子 Agent 样例，沿用父核心的主模型配置。插件页选“选择插件包目录”，核对预览后安装，在包详情启用“子 Agent 贡献”。第三方包沿用这套安装流程。

视觉核心是内置任务核心预设。在设置的模型页选择专项模型并启用，授权从下一次父运行生效。

| 样例 | 委派时提供 | 子实例提交 |
| --- | --- | --- |
| [规划者](../examples/worker-planner/README.md) | 原始目标引用、当前阶段、规划边界、可选的已有事实。可以不分配源码资源 | 备选步骤、各自的依据与依赖、仍需回答的问题 |
| [探路者](../examples/worker-scout/README.md) | 一个具体问题、调查范围、已有证据与尝试、可读取的资源 | 有来源及所见版本的观察、排除的路线、仍未证实的内容 |
| [视觉核心](../examples/worker-vision/README.md) | 授权目录内的源文件和转写目标 | 选图依据、纯转写、不同视图回执和易混字符 |

## 一次委派如何走完

1. 执行者需要帮助时选择 `use_tool_group({"group":"plugins"})`，再用 `inspect_worker_plugins({})` 取得本轮可用的 `worker` 身份和 Schema。目录内容可读时，仍可决定自己做。插件的方法正文随被委派的子实例加载。
2. 执行者从包内 `example.json` 取 `input` 的结构，按本次需要填写。调用 `delegate_plugin_worker` 时用发现结果中的 `worker`、本轮稳定的 `request_key`、`kind:"report"` 和本次确需的 `resource_ids`。成功回执给出 `delegation_id` 和 `operation_id`，随后可继续编辑、读码等独立工作。表中的三个样例均为只读报告委派。
3. 子实例在自己的历史和授权内工作，以 `submit_worker_result({"submission_key":"report-1","data":…})` 提交符合本包 `outputSchema` 的结果。每条调查观察应标明所见版本。`demand_revision` 标识要求版本，读码回执的 `revision` 标识文件内容，二者分别核对。证据不足时把局限写入结果。
4. 保存后的成果身份随父运行后续请求送达。执行者用 `manage_plugin_worker` 的 `result` 读取具体 `data`，再用 `receipt` 记录 `accepted`、`partial`、`deferred` 或 `rejected`。例如旧版本的观察仍可作为历史参考，当前实现已变时可以拒绝采用。处置与实际修改、检查各有各的动作记录。

下面的操作复用同一工具 `manage_plugin_worker`。尖括号代表先前真实回执中的值。

```jsonl
{"action":"message","delegation_id":"<delegation_id>","request_key":"material-1","text":"补充一条已经核过的证据及来源。"}
{"action":"resume","delegation_id":"<delegation_id>","request_key":"continue-1","text":"根据补充材料接着上次调查。"}
{"action":"result","delegation_id":"<delegation_id>","result_id":"<result_id>"}
{"action":"receipt","delegation_id":"<delegation_id>","result_id":"<result_id>","request_key":"decision-1","disposition":"partial"}
{"action":"cancel","delegation_id":"<delegation_id>"}
```

`message` 适用于仍活跃的委派。`resume` 适用于同一父运行内已退出、有历史且允许继续的委派。`cancel` 后可 `query` 核对真实终态。停用或卸载从插件页操作，随后的执行重新校验授权。

## 错误和重试

| 观察到的事实 | 调用方如何处理 |
| --- | --- |
| `worker_data` | 对照本包 `inputSchema` 或 `outputSchema` 核对字段，再决定是否重试 |
| 同一键、相同输入重复送达 | 使用原委派或原成果 ID。改变同键对应内容会收到 `request_changed` |
| 读取被拒或资源不在委派内 | 保留已知证据和未解项，由执行者决定是否缩小问题或另行提供获准材料 |
| 子运行 `failed` 且 `results` 为空 | 报告实际失败原因。父方可以独立收尾，不能记成已收到或采纳的成果 |
| 已出现 `result_id` 但父方尚未处置 | 可先做其他工作。确需使用时读取对应版本并作出自己的决定 |

实现新的包时，可从规划者或探路者开始，复制目录并替换名称、方法和 Schema，更新一对输入输出实例。用普通插件安装入口预览并启用，再测试正常结果、拒绝结果和停用。宿主提供 L0、授权、历史和成果存储。需要跨运行收回成果时，在父会话的运行偏好中选择手动续接或有新成果时唤醒。

契约字段及限制见[公开草案](worker-plugin-contract.md)。可以复用[包与 Schema 固定测试](../tools/test_worker_examples.py)和[后台 Electron 验收](../tools/accept_worker_examples.cjs)。
