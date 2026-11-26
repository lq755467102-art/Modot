# Windows 桌面实机内存

这一页保留早期九次桌面快照。同一份工程、同一道题的三阶段对比另有完整样本和采样器。[查看同题桌面实测](matched-desktop-memory.md)

2026-11-23 在一台 Windows 11 x64（内部版本 26300）上，对已经打开的三个应用同时取样。Codex 正在执行任务。WorkBuddy 和 Modot Pilot 沿用已经打开的用户会话，窗口数、运行时长、项目和插件没有配平。各列分别取九次采样的中位数，单位为 MiB。

| 应用与本机版本 | 进程数 | 私有工作集 | 工作集合计 | 私有提交内存 |
| --- | ---: | ---: | ---: | ---: |
| Codex 桌面，应用包 26.1117.4957.0 | 38 | 1,213 | 2,491 | 2,739 |
| WorkBuddy 5.11.3 | 24 | 472 | 1,356 | 3,135 |
| Modot Pilot，已打开的会话 | 7 | 229 | 579 | 573 |

按私有工作集看，九次的范围分别为 1,208–1,244、471–472 和 226–229 MiB。Windows 的工作集包括驻留的共享页，跨进程相加可能重复计算。私有工作集只计私有驻留页，适合观察一个应用独占的物理内存。私有提交内存还包括当前未驻留的分配。不同计数器可以同时给出相差很大的读数。[Windows 内存计数器](https://learn.microsoft.com/en-us/windows/win32/memory/memory-performance-information) · [工作集定义](https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.process.workingset64)

采样从桌面主进程开始递归统计子进程，包括渲染器、GPU、应用服务和随产品启动的工具。分属系统服务或已经脱离进程树的服务独立计量。采样器自身从树中排除。每次读取同一组系统计数器，两次采样之间停三秒。另检查了最近两天选取的运行诊断日志尾部。WorkBuddy 的主进程和守护进程分别记录 RSS，这些记录缺少同一时刻的完整应用进程树。抽查的 Codex 日志未找到可对比的整树计数。

这组观测支持「这台机器在当时打开的 Modot 会话占用较少」。要把优势归于框架，还需要统一仓库、任务、窗口和启用组件，分别测冷启动、空闲、长会话和构建峰值。新预览版在隔离用户目录中的启动、资源开销和实机任务另有记录。[本版验证](preview-validation.json) · [九次脱敏采样](desktop-memory.json)

在 PowerShell 中传入桌面主进程的 PID，可在另一个工作站重复此测量。多个 PID 共享一组快照，脚本输出字节数。

```powershell
$apps = @(Get-Process -Name WorkBuddy | Where-Object MainWindowHandle -ne 0)
.\tools\measure-process-tree.ps1 -RootProcessId $apps[0].Id -Samples 9 -IntervalSeconds 3
```
