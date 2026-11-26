# 委派插件公开契约 `modot.worker/1-draft`

当前实现使用 `modot.worker/1-draft`。[官方样例、视觉核心预设](official-worker-examples.md)和第三方包共用原生 L0 与这份公开草案。稳定版本仍需后续生命周期验证。架构见[任务核心与委派](modules.md#任务核心与委派)，机器可读声明见 [Schema](schemas/worker-plugin-v1.schema.json)。

## 1. 包声明与注册

在现有 `plugin.json` 增加 `workers`，每包最多8项。继续使用现有预览、指纹核对、保留副本、安装/卸载、授权快照和作用域工具注册。不扫描或执行包中的 Python/JS；`method` 只是当前委派的方法材料。

```json
{
  "name": "example-research",
  "version": "0.1.0",
  "workers": [{
    "name": "investigate",
    "protocol": "modot.worker/1-draft",
    "description": "调查一项已授权问题，报告依据和未确定项",
    "method": "只调查父方明确提供的问题与范围。使用实际来源说明结论，缺少证据时说明局限。",
    "requires": ["native-l0", "independent-history", "structured-result"],
    "optional": ["messages"],
    "inputSchema": {
      "type": "object", "properties": {"question": {"type": "string"}},
      "required": ["question"], "additionalProperties": false
    },
    "outputSchema": {
      "type": "object", "properties": {"finding": {"type": "string"}},
      "required": ["finding"], "additionalProperties": false
    }
  }]
}
```

安装后 `workersEnabled=false`。插件详情按声明显示“启用子 Agent 贡献”或“启用任务核心贡献”。内置视觉核心也可在模型设置中启用。既有宿主接口也支持 `POST /api/v2/plugins/{id}`，参数为 `expectedRevision`、`workersEnabled` 和启用时的 `confirm:true`。新启用从下一轮进入授权快照；停用、卸载或授权版本变更即时阻止后续调用。包文件保持不可变，升级安装另一个指纹版本。

预览和安装会核对 `requires` 与协议版本。发现结果的 `unavailableOptional` 列出尚未支持的可选能力。宿主支持 `native-l0`、`independent-history`、`messages`、`resume`、`cancel`、`structured-result`、`read-code`、`isolated-write`、`isolated-check`、`cross-parent-run` 和 `automatic-wakeup`。每次执行还需满足资源、父授权、生命周期设置与隔离方式。

输入根类型为 object。输入/输出 Schema 支持 type、properties、required、additionalProperties（仅布尔）、items、enum、description，以及适用于对应类型的长度/数量/数值上下界。默认额外对象字段不允许。拒绝未知关键字、外部引用和超过8层的 Schema；不用“忽略未知字段”冒充兼容完整 JSON Schema。

## 2. 模型入口与身份

未声明 `modelRole` 的贡献作为子 Agent，沿用父核心的主模型配置。声明 `modelRole: "vision"` 的贡献作为任务核心，使用用户在模型设置中选中的视觉核心连接。模型身份按服务和模型 ID 核对，兼容端点按端点和模型 ID 核对。派发时绑定当前端点和模型，后续请求继续核对绑定与授权。选型缺失、与父核心相同或连接已变化时，宿主会返回对应错误。

工具注册使用现有请求作用域。有获准的委派贡献时，父 L0 直接取得发现、委派和管理入口。其他插件工具可按需通过 `plugins` 工具组取得。发现结果提供包身份、能力及输入输出 Schema，`method` 正文只随实际委派的子实例加载。包内 `plugin.json` 和安装预览接口保留完整声明。

| 入口 | 参数 | 语义 |
| --- | --- | --- |
| `inspect_worker_plugins` | 无 | 返回本轮获准贡献、精确 worker 键、协议、输入输出 Schema 和能力差异。`core_type` 为 `sub_agent` 或 `task_core`，后者同时给出可用性与当前模型。 |
| `delegate_plugin_worker` | worker、request_key、input、resource_ids、kind；可选 expected_revision、model_calls | kind 为 report/code。立即保存并返回委派/异步操作身份。code 需要现有写权限和当前项目版本，继续复用子任务隔离副本 |
| `manage_plugin_worker` | delegation_id、action，及下表对应字段 | 查询、消息、续接、取消、读取成果、记录处置；查询可显式等待 |
| `submit_worker_result` | submission_key、data | 仅在实际插件子实例中注册；data 按该贡献的 outputSchema 校验。保存不结束运行，不证明父任务完成 |

宿主复用已有 `/api/agent-tools`、每运行能力令牌、真实动作身份和请求回执。对应操作名为 `worker-discover`、`worker-delegate`、`worker-manage`；不增加匿名 RPC 服务或另一个 MCP 服务端。开发者通过包声明和上述工具工作，不直接伪造宿主令牌。

| 身份 | 来源和用途 |
| --- | --- |
| `worker` | 发现入口返回的 `plugin_id/name`，不得把包显示名当作路由 |
| `delegation_id` | 原子任务记录的32位身份，整个委派不变 |
| `operation_id` | `wop-…`，每次明确续接生成新值；异步查询使用 delegation_id |
| `child_session_id` / `child_run_id` | 独立历史容器 / 一次实际运行；续接沿用会话，更换运行 |
| `logical_task_id` / `demand_revision` | 父逻辑任务及委派时要求版本，成果保留适用版本 |
| `result_id` | `wres-…`，不可变成果版本；不能用 actionId 或 operationId 代替 |
| actionId | 真实工具动作证据，由原工具链生成，不作为成果或委派身份 |

同一父运行的同一 request_key 和相同规格返回原委派，不重复分配额度、创建会话或派发。规格改变返回 `request_changed`。不同委派不共用历史。昵称仅用于展示。

## 3. 消息、续接和取消

| action | 附加字段 | 行为 |
| --- | --- | --- |
| query | 可选 wait_ms（0–30000，默认 0） | 返回真实状态、当前操作、子运行、结果摘要及消息数量。等待在成果保存、运行到达终态、父运行停止或时限到期时结束 |
| message | request_key、text | 当前活动委派的补充材料；下一个已有模型组装边界进入子历史，不并发改写在途请求 |
| resume | request_key、text | 确认前进程已退出并有历史后，显式续接同一子会话/隔离目录；不清空历史、用量或结果 |
| cancel | 无 | 幂等请求停止；stopping/unknown 不冒充进程已退出。退出已确认后才释放原预算保留 |
| result | result_id | 读取原始结构化成果；不修改采用状态 |
| receipt | request_key、result_id、disposition | 记录父方 accepted/partial/rejected/deferred 声明；不自动合并、验收或调度 |

每个操作拒绝不属于自己的字段。message/resume/receipt 的 request_key 在整个委派内去重，同键不同内容拒绝。取消后的委派不会被晚到续接重新启动。首次启动未留下可核对历史、或仍存在活动/身份不明进程时拒绝续接；需要新任务时使用新委派，而不是盲重放。

在会话运行偏好的「委派存续方式」中选择父运行结束后的处理。宿主将选择冻结到本轮委派，公开回执的 `lifetime` 返回生效值。

| workerContinuation | 父运行成功存档后 | 晚到成果 |
| --- | --- | --- |
| parent-run，默认 | 结束活动委派 | 保留已提交的记录供审查 |
| manual | 由本机后端继续运行原子进程 | 手动续接同一逻辑任务时送达 |
| wake | 与 manual 相同 | 在条件满足时自动续接父任务一次 |

跨运行能力名为 `cross-parent-run` 和 `automatic-wakeup`。若包必需的能力超出本次授权，委派会被拒绝。同一逻辑任务中重复发送相同委派键和规格会取回原委派。需求修订会同时显示成果的原需求版本和当前版本。隔离写入的成果记录候选版本，读取活动目录的报告标为 `live-read`，有调用方提供的版本时一并保留。执行者决定是否需要重新核对。新任务使用新的任务身份。

自动唤醒要求父运行已成功退出、同一任务和需求版本、原权限及运行配置继续有效，并有尚未送达的成果。用户已排队的输入优先。每次原父运行至多自动续接一次，唤醒产生的后续委派使用手动回收。设置了调用或轮数额度时使用手动回收，避免创建新运行重置额度。用户停止或归档会终止活动委派并禁止其唤醒。

## 4. 成果、送达与采用

成果先原子保存到现有子任务行，再返回工具回执。同一操作内 submission_key 相同且数据相同返回原 result_id；内容改变拒绝。不同操作产生新成果，旧版不覆盖。

保存、进入请求和后续处置分别记录：

1. `results[].data`、来源动作/运行、哈希及要求版本：成果已保存。
2. `delivery.readActionId`：父方请求读过；不等于已发给模型。
3. `delivery.notifiedIn`：只有成果身份通知进入父请求；`delivery.includedIn`：完整结构化成果正文在真实工具回执中进入父请求。通知不冒充正文；若大回执被截取或外存，也不把片段算作完整送达。这些字段不宣称网络一定成功、更不宣称模型已经理解或采用。
4. `disposition`：父方明确采用处置及其动作来源；缺少时保持 null。

运行结束和成果提交相互独立。子运行正常结束但未调用提交工具时，query 返回 `result_status=not_submitted`，不会把自由文本假造为通过 Schema 的成果。保存后、组装前崩溃，恢复可重新提供未确认通知；请求记录后的网络异常由原 Provider/历史处理，不触发额外模型或副作用重放。

父核心需要逐字写入文件时，可用 `write_project_file` 的 `parts` 按顺序组装正文。每项提供父核心补充的 `text`，或提供 `delegation_id` 和 `result_id` 引用已读取成果的 `data.transcription`。局部复核后，可以按零基字符区间 `start`、`end` 选用不同版本的对应部分，右端不包含在内。每次写入至少引用一个成果，`parts` 与整文件正文 `text` 二选一。

当前的逐字候选读回时附带 `writeParts`，父核心验收后默认把它交给写入工具。已用 `receipt` 确认采用且只有一份可用候选时，可省略正文，宿主直接引用该版本。原样提交已读正文时宿主也会记录来源，按提交保留末尾换行。新建逐字文件时，如果普通正文与当前已读的单行候选仅有少量字符差异，写入会返回差异位置并要求选定来源片段。确认候选不适用时可记录 `rejected`。

复核得到新证据后，可引用原版本的前后区间，并仅在中间换用新成果的对应区间。候选的 `partial` 和不确定标注用于确定复核范围，实际采用由父核心选择。


引用需要匹配当前父任务和需求版本。宿主核对来源的现有读取权限及目标的写权限，使用当前文件修订写入。已拒绝的成果需要重新作出采用决定。正文按片段顺序原样编码为 UTF-8，换行随片段显式提供。写入后核对文件字节，回执的 `resultSources` 关联文件修订、引用的成果版本及字符区间。

每个委派最多保留64条补充、32项结构化成果、128项写操作去重记录，受原512KiB子任务记录容量共同约束；输入8KiB、单成果32KiB。满时明确失败，原证据不丢弃。这些是存储容量，不是模型调用次数限制。大正文使用现有证据/成果工具保留，结构化结果引用它。

## 5. 上下文、权限与恢复

子实例共用 NativeEntry、ExecutorCore、工具发现、压缩和存储，保持131072配置；不限调用仍由原预算表达，续接不重置已用次数。只选择明确委派输入和资源，不复制父方完整消息数组。方法经已有 PromptFragments 注册为插件材料，输入标明父方拆解来源；补充有真实动作来源，不升级为用户权限。

普通 report 沿用只读资源；code 沿用现有隔离副本及当前版本保护。贡献未声明 isolated-write/isolated-check 时拒绝相应动作。所有资源访问、写入、执行仍经共同底座检查；主目录不因发布成果被改写。整合使用现有 prepare/apply_subtask_integration，冲突/用户改动保护不变。子实例不能递归委派、安装/管理插件或继承父插件连接。

撤权/停用每次调用重新检查，后台工作者也会请求停止。已有结果和历史保留；读取仍校验所属逻辑任务及当前资源读取权限。不能用停用后保留的旧句柄继续写入或发布。

强退恢复核对 runtime.json、session.json 和委派行。能确认的终态恢复为实际状态，缺少终态时显示 unknown。已提交的成果可以按原 result_id 读回。自动续接先在父启动锁内记下领取，领取与启动之间强退时留给用户手动续接。显式续接子运行要求原授权、相同需求版本、前进程已退出且原历史可核对。

## 6. 错误与开发验证

| 类别 | 代表 code | 调用方处理 |
| --- | --- | --- |
| 声明/数据 | worker_protocol、worker_schema、worker_manifest、worker_data、worker_arguments | 核对声明和参数，不能自动改权限 |
| 能力/权限 | worker_capability、plugin_denied、worker_revoked、resource_denied、resource_revoked | 真实授权变化；旧句柄不可绕过 |
| 去重/状态 | request_changed、worker_active、worker_inactive、worker_cancelled、worker_unknown | 查询原身份；不盲重放 |
| 身份/寿命 | worker_owner、worker_result、worker_lifetime、parent_owner_lost | 核对归属、ID类型及支持范围 |
| 容量/额度 | worker_capacity、task_record_large、insufficient_budget | 保留证据；通过已支持的显式操作处理 |

最小验证包可采用上面的纯数据示例。公开契约有固定的清单、注册、授权和结果用例。[契约测试](../tools/test_worker_plugins.py) · [规划者](../examples/worker-planner/) · [探路者](../examples/worker-scout/)
