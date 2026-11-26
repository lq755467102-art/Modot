# Desktop memory on one engineering task · 同工程桌面内存实测

On 2026-11-23, all three desktop clients fixed the same generated file and reproduced the same change in the checker’s exit status, from 1 to 0. The largest private resident footprint sampled during the task was **275.2 MiB for Modot, 1,481.2 MiB for Codex and 1,518.3 MiB for WorkBuddy**. Each client has one eligible run. The medians describe time within that run.

三端在同一台 Windows 11 x64 上打开逐字节相同的工程副本，收到完全相同的中文题面，通过桌面聊天调用各自的真实模型。下面同时给出独占物理内存、工作集合计和私有提交内存。每个单元格都是**该阶段的中位数 / 采样峰值**，单位为 MiB（1,048,576 字节）。[逐秒原始数据与交付核验](matched-desktop-memory.json) · [PowerShell 采样器](sample-desktop-memory.ps1)

## Results · 三个阶段

| Client | Phase 阶段 | Seconds | Samples | Private working set 私有工作集 | Total working set 工作集合计 | Private commit 私有提交 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Modot | Startup 刚启动 | 20.000 | 20 | 143.1 / 146.2 | 449.6 / 452.4 | 287.2 / 290.1 |
| Modot | Project 打开工程 | 20.000 | 20 | 166.9 / 170.3 | 477.2 / 479.9 | 321.0 / 337.2 |
| Modot | Task 执行任务 | 41.121 | 41 | 249.1 / 275.2 | 597.9 / 626.1 | 413.4 / 440.1 |
| Codex | Startup 刚启动 | 20.000 | 19 | 558.2 / 1,628.4 | 1,002.5 / 2,608.2 | 920.5 / 2,445.0 |
| Codex | Project 打开工程 | 20.000 | 20 | 1,105.4 / 1,110.0 | 2,106.5 / 2,111.1 | 1,668.1 / 1,672.2 |
| Codex | Task 执行任务 | 104.245 | 102 | 1,340.0 / 1,481.2 | 2,580.3 / 2,852.2 | 2,093.1 / 2,530.9 |
| WorkBuddy | Startup 刚启动 | 20.000 | 19 | 1,184.6 / 1,189.5 | 2,098.8 / 2,103.9 | 1,769.4 / 1,808.7 |
| WorkBuddy | Project 打开工程 | 20.000 | 19 | 822.3 / 825.8 | 1,768.3 / 1,771.0 | 1,353.8 / 1,365.5 |
| WorkBuddy | Task 执行任务 | 61.148 | 60 | 1,405.8 / 1,518.3 | 2,728.8 / 2,859.7 | 2,191.3 / 2,320.5 |

The test machine ran Windows 11 x64 build 26300.9711 with 31.7 GiB of OS-visible RAM. Each client was launched as a fresh process with a separate desktop profile. The OS cache stayed warm. Tasks ran in sequence.

| Client | Model shown or configured | Access during the run |
| --- | --- | --- |
| Modot Preview 0.2.0-preview.1 | DeepSeek Flash | Read/write in the project. Operations outside its scope require approval. |
| Codex desktop 26.1117.4957.0 | GPT-6.2 Sol Max | Full access |
| WorkBuddy 5.11.3 | Hy4 flash | Full access |

Codex and WorkBuddy loaded the signed-in account’s components and history. Modot used a fresh profile with one model configured. The figures include the clients’ local work to run their different models and tools. They exclude provider-side compute. Longer chats, large builds, additional extensions and different permission settings can change the ordering and footprint. The task durations describe how long we observed memory in this run. They are insufficient to rank execution speed.

## Measurement · 怎样取样

1. **Startup** covers the first 20 seconds from desktop process launch. It includes initialization and any short spikes sampled during that window. **刚启动**从新进程启动计时，包含应用初始化，系统缓存保持预热。
2. **Project** covers the first 20 seconds after the checkout was confirmed in the UI, before sending the prompt. **打开工程**从桌面界面已选中指定工程开始，留足 20 秒才发题。
3. **Task** starts at the Send action and ends at the final assistant answer timestamp recorded by that client. An intervening command can finish earlier. **执行任务**从点击发送算到产品记录最终答复，排除之后的空闲时间。

For each sample the collector discovers descendants of the desktop root process and sums Windows process counters across that family. Renderer, GPU, app service and tool subprocesses count while they are members of the tree. The newly launched Codex app server and WorkBuddy daemon were verified in their respective trees. Detached system services and the observer itself are outside the totals. Peak means the largest sampled whole-tree sum. The target cadence was one second and each sample carries its collection time and midpoint offset. A short-lived process or a subsecond spike can fall between samples.

私有工作集是当时驻留在物理内存里的私有页。工作集合计还包含共享页，多个进程可能重复计入同一页。私有提交内存包含已经分配但当时未驻留的页。以本次任务为例，Codex 和 WorkBuddy 的峰值按私有工作集约为 1,500 MiB，按工作集合计分别是 2,852.2 和 2,859.7 MiB。两个读数回答不同的问题。[Windows 内存计数器](https://learn.microsoft.com/en-us/windows/win32/memory/memory-performance-information)

## Workload and grading · 题面与独立评分

We used the public candidate source at `ec8d6d5d`. The checkout had 626 tracked files including a small `verify.cmd` wrapper and the same project-local CPython 3.13.17 on each client. We changed one declaration in `desktop/renderer-v2/src/generated.ts` from `WorkroomProject` to `WorkroomProjectDraft`, leaving the protocol definition untouched. Each client started on `bench/protocol` with the same faulty tree, interpreter files and prompt. The JSON includes the input tree, interpreter, prompt and expected output hashes.

> 这个工程的工作间协议生成物有一处与定义不一致。请先在项目根目录实际运行 verify.cmd 复现，找出并修复漂移，只改需要修的生成文件，再运行同一个 verify.cmd。最后告诉我改了哪个相对路径、两次退出码和测试结论。不要安装依赖，不要提交。

The English equivalent is “A generated workspace protocol file has drifted from its definition. Run verify.cmd from the project root to reproduce the issue, fix only the generated file, and run the same command again. Report the relative path, both exit codes and the outcome. Leave dependencies and commits alone.” All clients received the Chinese original.

An independent control returned 1 with the drift in place and 0 with the known-good file. For each product we verified the two command receipts, compared every tracked file with the input, checked that the expected file matched the known-good bytes, verified that HEAD and the index were unchanged, and reran the generator check outside the agent. Codex and WorkBuddy retained both command exits in the wrapper log. For Modot the failed shell ran in a task copy and its incidental log was not written back, so we used the two persisted project-shell results. All three passed these checks. The outcome concerns protocol consistency. It does not establish that the entire project test suite passed.

评分器在三个客户端之外单独工作。它核对实际命令退出码、全部跟踪文件和提交状态，再独立运行协议检查。三端均只改了同一个生成文件。预备诊断有一次未装齐组件的解包运行失败，另一次已安装的 Modot 运行虽然完成了任务，但输入里的换行符与另外两家略有差异。表格使用同一源码、运行时和逐字节一致的输入工程完成的三次正式运行。

## Reproducing the comparison · 如何复测

1. Make three independent checkouts of the source revision above, use the same writable branch and apply the one-line drift. Give each checkout the same CPython and a `verify.cmd` that runs the generator with `--check`, records its exit code and returns it unchanged. Check the negative and positive controls first. Record your own versions, models, permissions and loaded extensions.
2. Start the memory collector from a separate PowerShell process for each newly launched desktop root. Record the launch, project selection, send and final answer times. Stop sampling after the answer. The example below records up to five minutes.
3. Keep samples whose midpoint lies in each phase. Convert bytes to MiB, then take the median and maximum of each series. The JSON in this directory includes all 320 in-window samples and their offsets for independent recalculation.

The command wrapper in all three checkouts was the following file, saved with Windows line endings. The project-local interpreter and the wrapper log were ignored by Git.

```bat
@echo off
setlocal
cd /d "%~dp0"
".bench-python\python.exe" -B tools\generate_workbench_protocol.py --check
set "code=%errorlevel%"
>>".bench-runs.log" echo %date% %time% %code%
exit /b %code%
```

For the sampling command, set `$desktopProcess` to the newly launched app’s root process and keep the collector in a separate PowerShell. It writes one record per sample.

```powershell
.\docs\benchmarks\sample-desktop-memory.ps1 -RootProcessId $desktopProcess.Id -Samples 300 |
    Set-Content -Encoding utf8 desktop-memory.jsonl
```

复测时请记录系统构建、已启用的组件和审批模式。要判断某个客户端的典型占用，还需要多次运行、轮换顺序，并覆盖大工程和长会话。仓库中另有一份较早的九次采样，保留了三个应用在各自既有任务下的读数。[早期桌面观测](desktop-memory.md)
