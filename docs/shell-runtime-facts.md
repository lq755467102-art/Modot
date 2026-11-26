# L0 命令、工作间与运行事实

L0 保留 `run_project_check` 协议名称，作为唯一主要命令入口。普通 argv、临时 Python 脚本和已保存工作间场景共用 `code_service → workbench.launch → safe_executor`。场景在宿主展开，不增加一次模型转交。不另建 Shell、验收或后台任务调度器。

## 命令与环境

- `argv` 执行普通程序；系统 Windows PowerShell 使用 `powershell.exe -NoProfile -NonInteractive -Command ...`。
- `script` 直接传 Python 正文，仍使用现有隔离材料投递。普通调用不需要工作间工具组。
- 反复使用的场景、环境引用继续按需加载 `workroom` 组。`environment_id` 复用的是准备环境，不是旧命令结果。`new` 显式创建保留环境。
- 每次 L0 执行调用真实运行；不再让同参数的历史成功或失败回执代替新执行。其他旧模式保留原行为，并显式返回 `reused` 和原动作身份。
- 默认源码只读；`writable_copy` 只允许写隔离副本。回写用户工程继续走已有授权、版本校验和保护，不能由快照、摘要或插件声明启用。
- 当前适配 argv、Python 和系统 Windows PowerShell。Bash、PowerShell 7、WSL 与交互式终端未因本次改动获得新的隔离支持。

## 回执与上下文

命令回执保留实际进程、源版本、执行目录、开始/结束、退出码、超时/取消、原日志和测试报告。`execution` 说明实际命令状态；`verification`/`assessment` 保留对应检查范围及报告，`requirementAcceptance=not_inferred`。退出码0和打印输出不能替代用户必验项或真实断言。

新 L0 隔离命令分流留存 stdout/stderr，`read_check_output(stream="stdout"|"stderr", after=...)` 按流读取；省略 stream 读取兼容的合并日志。合并日志是两流到达顺序，不保证还原命令内部跨流写入顺序。旧合并日志不能伪装成独立输出流。输出容量和缺失警告沿用原上限。

自动工作状态只提供版本、显式排障和记录索引，不再注入最近8条检查表。原始回执、失败与显式用户要求仍保留；需要完整记录时调用 `read_check_queue`。兼容检查视图仍可用于显式查看，不参与默认反馈。

L0 正常最终回复不再被框架追加失败总数结论。真实执行状态未知、明确用户必验项和权限异常仍照原规则报告。容量摘要只提炼有来源的历史观察、模型实现选择和检查结果；模型曾声称完成可以作为历史记录，不能被摘要升级成当前任务已完成的裁决或用户要求。131072 与既有容量触发规则不变。

只在模型投影中迁移有本地来源标注的旧宿主状态；原始持久历史不改。没有可靠来源的旧自然语言消息不按关键词删除。

## 具名运行事实

`loop.runtime_facts.RuntimeFacts` 为受信任的宿主适配模块提供 `register(name, read, source=...)`。read 只返回模块已有数据，不做自动命令、仓库扫描、远端轮询或模型调用。异常投递为该来源不可用，不生成任务失败或待办。

首批来源：

| 名称 | 事实来源 |
|---|---|
| host.workspace | 当前可读授权代码资料、源路径及写入/执行授权 |
| host.permissions | 本轮冻结权限与当前撤权共同形成的权限上限、命令隔离模式 |
| host.runtimes | 已实现的适配器范围；依赖安装情况明确未自动探测 |
| host.workroom | 最近实际命令观察到的环境目录、模式、环境ID与来源版本 |

工作间条目是最近回执观察，不宣称目录已重新验证，也不是全部环境库存；完整环境列表/释放仍使用已有 `review_test_environment`。观察绑定实际工作区ID及源路径；同资料ID切换检出后不再投递旧目录观察。释放已观察环境会移除该条来源。

运行事实首次以完整快照进入请求。同一作用域内只追加发生变化的具名事实及需要移除的名称。授权或插件来源变化时按当前范围重建快照，并从模型视图移除失效范围的旧快照和增量。压缩时把已归档的增量合成检查点快照，续接时依据当前宿主状态重新校验。

复用现有消息、会话存储和上下文投影，不增加事实数据库；不占用项目长期事实记忆，也不充当需求队列。稳定数据不附轮询时间戳。

## 插件入口

首版只支持安装清单中的可验证静态 JSON 声明，不执行插件 getter。例：

```json
{"runtimeFacts":[{"name":"language","value":{"declared":"python","availability":"not_probed"}}]}
```

每个插件最多8项，总声明最多8KiB。本地名称受限、不能重复。贡献自动加上 `plugin.<installed-id>.` 命名空间，附包指纹、授权版本和 `plugin-declaration` 来源；无法覆盖 `host.permissions` 等宿主事实。值沿用现有凭据脱敏。

贡献仅来自本轮已捕获、仍有效且至少启用一个既有组件的插件。安装不等于启用；变更授权/版本、停用或卸载后旧贡献失效。事实声明不提供文件、网络、命令权限。观察型 Hook 继续观察，不改成模型指令。

动态运行时、编译器和媒体处理可在以后通过受信任适配器接入同一数据接口；本轮没有加载 DSH 原生组件、插件执行宿主或 IDE。

行为参考：[DSH runtime-context.ts，固定提交39163dd](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/core/agent-loop/src/runtime-context.ts)。本次为独立 Python 适配，没有复制 DSH 源码或引入其运行时；将来复制实质代码时需保留其 MIT 许可和版权。
