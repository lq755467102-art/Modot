# 插件贡献目录样包

这四个目录都是可单独交给 Modot 现有安装器的插件包。从仓库根目录运行校验器，可以在安装前查看包指纹、它提供的组件和需要修正的字段。[本地清单校验器](../../desktop/renderer-v2/tools/validate_plugin_manifest.py)

| 包目录 | 贡献 | 可复验的差异 |
| --- | --- | --- |
| [legacy-skill](legacy-skill/.codex-plugin/plugin.json) | 旧 Codex 清单和 `outline` 技能 | 省略版本和目录扩展时仍可识别，版本显示为 `unversioned`。 |
| [studio-v1](studio-v1/plugin.json) | 页面标识 `overview`、MCP 服务 `notes`、三种图标 | 安装后可在管理页启用纯数据样页。显式启用服务后，使用现有 Windows 隔离运行环境可以列出并调用只读工具 `read_note`。 |
| [namesake](namesake/plugin.json) | 页面标识 `overview`、通用图标 | 与工作室第一版同名、同版本、同页名，作者声明的包标识和内容指纹不同。 |
| [studio-v2](studio-v2/plugin.json) | 保留 `overview`，增加 `history`，撤去 `notes` | 普通安装可与第一版并存。显式替换核对第一版安装 ID、预期修订与第二版预览指纹。 |

## 运行校验器

使用项目的 Python 3.10+ 环境。读取带技能的包时需要已有工作台依赖 PyYAML。在 PowerShell 中可传一个目录或多个目录。默认输出供人阅读，加 `--json` 可供脚本消费。[工作台依赖](../../requirements-workbench.txt)

```powershell
$catalogChecker = 'desktop/renderer-v2/tools/validate_plugin_manifest.py'
$sampleDirs = (Get-ChildItem examples/plugin-catalog -Directory).FullName
python -B $catalogChecker @sampleDirs
python -B $catalogChecker --json @sampleDirs
```

[接口与工作样例](../../docs/protocols.md)
