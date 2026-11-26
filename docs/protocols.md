# 接口与最小示例

桌面渲染器通过受限 preload/IPC 调用本机 HTTP 后端。公开版默认端口为 18791，源码预览另用 18798。宿主按资料目录持有自己的连接令牌，授权访问和资源范围由服务端复核。项目的 JSON Schema 与类型生成器随源码提供。[桌面桥接](../desktop/bridge-v2.cjs) · [服务入口](../bin/server.py) · [工作间协议](../harness/workbench-v2.schema.json)

| 接口 | 调用与结果 | 可运行或机器可读的依据 |
| --- | --- | --- |
| 宿主连接 | GET `/api/health` 可作连通探针。GET `/api/v2/bootstrap` 返回工作台状态。桌面桥接为受保护请求带本机令牌。 | [workbench.py](../loop/workbench.py) · [TypeScript 生成器](../tools/generate_workbench_protocol.py) |
| 普通会话 | POST `/api/v2/conversations/threads` 创建会话。按 ID 查询和发送。运行、工具动作与用户可见消息分别保存。 | [会话 API 用例](../tools/test_conversations.py) · [投影与证据](../loop/evidence.py) |
| 工作间和会话 | GET `/api/v2/workroom?conversationId={id}` 返回当前会话可用的代码根和范围版本。POST `/api/v2/workroom/open` 携带会话 ID、资源 ID 与范围版本打开所选检出目录。 | [检出绑定](../loop/project_workroom.py) · [工作间 Schema](../harness/workbench-v2.schema.json) |
| 附件 | 对 `/api/v2/conversations/threads/{id}/attachments` POST 文件名和 Base64 内容，得到附件 ID。通过该引用读取表格工作表及行范围、图片或指定时间的帧。移除后需重新核对访问权。 | [附件协议用例](../tools/test_attachments.py) · [工作表与行定位](../tools/test_documents.py) · [帧定位](../tools/test_video_attachments.py) |
| 插件与工具 | `plugin.json` 描述贡献，MCP 清单描述服务。安装、启用和单次执行各有状态。读取工具的实际输出保留动作 ID。 | [只读 MCP 样例](../examples/plugin-catalog/studio-v1/mcp.json) · [服务实现](../examples/plugin-catalog/studio-v1/notes.py) |
| 独立页面 | 页面由版本化数据描述。包身份和页面键共同区分同名页面。 | [页面 Schema](../docs/contracts/plugin-data-page-v1.schema.json) · [样页](../examples/plugin-catalog/studio-v1/data-pages/overview.json) |
| 大核插件 | 工作者声明 `modot.worker/1-draft`、所需能力和输入输出结构。宿主管理委派与回收。 | [工作者协议](worker-plugin-contract.md) · [Schema](schemas/worker-plugin-v1.schema.json) · [规划者样例](../examples/worker-planner/plugin.json) |
| 记忆实现与带入 | GET `/api/v2/plugins/memory` 返回已安装实现与当前选择。POST 携带 `providerId`、`expectedRevision` 和确认字段更新选择。会话的 `/memory` 接口读取该会话按范围选出的记忆。 | [记忆适配器](../loop/memory_providers.py) · [会话便签](../examples/memory-notes/plugin.json) · [切换与撤销用例](../tools/test_memory_providers.py) |
| 计划和编辑器宿主 | 调度和应用按各自注册接口工作。格式化预览绑定草稿版本。 | [计划路由](../loop/scheduled_tasks.py) · [格式化槽位用例](../tools/test_app_formatting_slot.py) |

一个最小的附件输入是 `{"name":"sample.csv","data":"YSxiCjEsMgo="}`。这表示有两列的 CSV。导入成功后可按返回的 ID 和 `sheet-1-row-2` 读取数据行。动作已完成、文件已读、图像进入本次模型请求是三个可独立核对的事实。

在仓库根目录运行以下命令，可检查生成的工作间协议是否与源码一致，再验证附件入口。用开发指南准备好的 Python 环境执行。[开发指南](install.md)

```powershell
python tools/generate_workbench_protocol.py --check
python -m unittest tools.test_documents tools.test_image_event_cards tools.test_public_preview
```
