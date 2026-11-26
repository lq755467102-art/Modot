# Ship a first Modot tool · 给 Modot 接一个工具

The [review-notes example](../examples/plugin-catalog/studio-v1/) is a complete, read-only MCP service. It runs locally and needs only standard Python to answer requests on stdin. Modot supplies the package installer, connection, permissions and chat UI. You can edit the service and see the result without writing an agent loop.

[审阅便签样包](../examples/plugin-catalog/studio-v1/)提供可直接运行的只读 MCP 服务。它从标准输入读取请求，用标准 Python 就能回复。Modot 负责安装、连接、授权和在会话中展示工具结果。下面的例子先核对服务自己的返回，再把同一目录接进桌面。

## 1. Inspect the three pieces · 读懂三个文件

| File | English | 中文 |
| --- | --- | --- |
| [plugin.json](../examples/plugin-catalog/studio-v1/plugin.json) | Gives the package its name, version and catalog contributions. | 描述包名、版本和页面等贡献。 |
| [mcp.json](../examples/plugin-catalog/studio-v1/mcp.json) | Launches the `notes` service with `python ./notes.py` in the package. | 声明服务名、启动程序和包内的相对脚本路径。 |
| [notes.py](../examples/plugin-catalog/studio-v1/notes.py) | Defines the `read_note` input schema and the text that an actual call returns. | 定义工具名、参数约束，以及调用时真正返回的文本。 |

## 2. Exercise the protocol · 发一次完整请求

Run this in PowerShell from the repository root with Python on PATH. The initialization notification has no reply. The three replies with IDs 1, 2 and 3 show the negotiated protocol, the tool declaration, and the note itself.

在仓库根目录用 PowerShell 执行，确保当前环境可调用 Python。编号为 1、2、3 的三条回复依次给出协议版本、工具声明和实际便签。初始化完成通知无需回复。

```powershell
@(
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"notes-demo","version":"1"}}}'
  '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_note","arguments":{}}}'
) | python -B examples/plugin-catalog/studio-v1/notes.py
```

The last reply contains `"content":[{"type":"text","text":"Review note from studio v1."}]`. An unexpected argument to this tool produces JSON-RPC error `-32602`. This gives you a quick positive and negative check while developing. 最后一条返回便签原文。把空参数对象改成 `{"extra":1}` 即可验证输入错误的分支。

## 3. Make it yours · 改成自己的服务

Copy the package to a new directory. In `notes.py`, change the tool name and description in `TOOL`, then change the text in the `tools/call` response. Repeat the call using the new name and check the new text. Give a distributable package its own name and package key. If you keep the sample page, use the same new package key in the page declaration. [Page declaration](../examples/plugin-catalog/studio-v1/data-pages/overview.json)

复制样包，在 `notes.py` 的 `TOOL` 中改工具名和说明，再改 `tools/call` 返回的文字。使用新名称重发请求，核对文字确实改变。要作为自己的包分发，换上自己的包名和包标识。保留样例页面时，同步更新页面声明中的包标识。

## 4. Connect it to a conversation · 接入真实会话

1. In **Plugins**, choose the local package directory, inspect its contributions, install it and enable its MCP service. On Windows the example uses Modot's isolated runtime. 在「插件」选择本地包目录，预览后安装并启用 MCP 服务。
2. In a project chat, ask the agent to read the review note. Open its tool receipt and check the returned text. The manifest validator only inspected the package declarations. 让 Agent 读取审阅便签，点开对应操作核对返回内容。清单校验和实际调用分别验证安装与运行两步。
3. Disable the service and use a new request to check that the tool is unavailable. A read-only hint documents intent. The host still enforces permissions. 停用后另发一次请求，核对可用状态。只读提示描述用途，实际权限由宿主执行。

For workflows expressed in prose, see the [Skill example](../examples/plugin-catalog/legacy-skill/skills/outline/SKILL.md). For a worker that owns a subtask and reports evidence to a parent, see the [scout example](../examples/worker-scout/example.json) and [worker contract](worker-plugin-contract.md). 选扩展类型时，先确定交付的是一套方法、一次工具调用，还是一项独立调查。
