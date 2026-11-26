// 在工作间控制调试会话并轮询调试协议消息，驱动堆栈、变量和断点视图。
// 主要引用方包括 `Panels.tsx`。
import { useEffect, useRef, useState } from "react";
import { api, wsPath, loadJSON, saveJSON } from "./client";
import type { WorkspaceSummary } from "./domain";
// 工作间传入检出目录和打开的 Python 文件。
// 调试会话的身份按该工作区保留，协议事件由后端轮询提供。
export function DebugPane({
  workspace,
  blockedReason,
  file,
  line,
  onOpen,
  onError,
}: {
  workspace: WorkspaceSummary;
  blockedReason: string | null;
  file: string;
  line: number;
  onOpen: (p: string, l: number) => void;
  onError: (e: unknown) => void;
}) {
  const [id, setId] = useState(() => loadJSON("debug-" + workspace.id, "")),
    [sessions, setSessions] = useState<any[]>([]),
    [watch, setWatch] = useState(""),
    [phase, setPhase] = useState("未启动"),
    [messages, setMessages] = useState<any[]>([]),
    [frames, setFrames] = useState<any[]>([]),
    [variables, setVariables] = useState<any[]>([]),
    [stopped, setStopped] = useState(false);
  const thread = useRef<number>(0);
  const frameId = useRef<number>(0);
  const pendingVariables = useRef(new Map<number, string>()),
    watchRef = useRef(watch);
  watchRef.current = watch;
  useEffect(() => {
    saveJSON("debug-" + workspace.id, id);
    api(wsPath(workspace.id, "debuggers"))
      .then((r) => {
        setSessions(r.debuggers);
        if (id && !r.debuggers.some((d: any) => d.id === id)) {
          setId("");
          setPhase("后端已重启，原调试不可重连");
        }
      })
      .catch(onError);
  }, [id, workspace.id]);
  // 将命令发给原调试会话，返回请求序号以供响应匹配。
  async function command(command: string, args: any = {}) {
    const sent = await api(
      "/api/v2/debug/" + id,
      { command, arguments: args },
      true,
    );
    return sent.seq as number;
  }
  useEffect(() => {
    if (!id) return;
    let cancelled = false,
      timer: ReturnType<typeof setTimeout>,
      cursor = 0;
    // 按后端消息游标增量处理。
    // 停住时依次请求栈帧、作用域和变量；出现消息缺口时用仍可确认的停住线程恢复读取。
    async function poll() {
      try {
        const r = await api("/api/v2/debug/" + id + "?after=" + cursor);
        if (cancelled) return;
        cursor = r.next;
        setMessages((old) =>
          [
            ...(r.gap ? [] : old),
            ...r.messages.map((m: any) =>
              m.event === "output"
                ? {
                    ...m,
                    body: { ...m.body, output: m.body.output?.slice(-8192) },
                  }
                : m,
            ),
          ].slice(-500),
        );
        if (r.gap && r.phase === "stopped" && r.stoppedThread) {
          thread.current = r.stoppedThread;
          setStopped(true);
          setPhase("断点/暂停");
          await command("stackTrace", { threadId: r.stoppedThread });
        }
        for (const m of r.messages) {
          if (m.event === "stopped") {
            setStopped(true);
            setPhase("断点/暂停");
            setVariables([]);
            thread.current = m.body.threadId;
            await command("stackTrace", { threadId: m.body.threadId });
          }
          if (m.event === "continued") {
            setStopped(false);
            setPhase("运行中");
            setVariables([]);
          }
          if (m.event === "terminated" || m.event === "exited") {
            setStopped(false);
            setPhase("已退出");
          }
          if (m.command === "evaluate" && m.success)
            setVariables((old) => [
              ...old,
              { name: watchRef.current || "表达式", value: m.body.result },
            ]);
          if (m.command === "stackTrace" && m.success) {
            setFrames(m.body.stackFrames || []);
            if (m.body.stackFrames?.[0]) {
              frameId.current = m.body.stackFrames[0].id;
              await command("scopes", { frameId: frameId.current });
            }
          }
          if (m.command === "scopes" && m.success) {
            setVariables([]);
            for (const scope of m.body.scopes || []) {
              const seq = await command("variables", {
                variablesReference: scope.variablesReference,
              });
              pendingVariables.current.set(seq, scope.name);
            }
          }
          if (m.command === "variables" && m.success) {
            const group = pendingVariables.current.get(m.request_seq) || "展开";
            pendingVariables.current.delete(m.request_seq);
            setVariables((old) => [
              ...old.filter((v) => v.group !== group),
              ...(m.body.variables || []).map((v: any) => ({ ...v, group })),
            ]);
          }
          if (m.type === "response" && !m.success)
            onError(new Error(m.message || "调试请求失败"));
        }
        if (r.alive || r.hasMore)
          timer = setTimeout(poll, r.hasMore ? 20 : 300);
      } catch (e) {
        if (!cancelled) {
          onError(e);
          timer = setTimeout(poll, 2000);
        }
      }
    }
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [id]);
  // 先建立调试会话并按协议走完初始化和断点配置，再启用日常轮询。
  async function start() {
    if (blockedReason) return;
    try {
      if (!file.endsWith(".py")) throw Error("请先打开要调试的 Python 文件");
      const created = await api(wsPath(workspace.id, "debuggers"), {});
      const debugId = created.id;
      const send = (command: string, args: any) =>
        api("/api/v2/debug/" + debugId, { command, arguments: args });
      let cursor = 0;
      // 初始化阶段沿新会话游标等待指定协议确认，限定等待时窗。
      async function until(check: (m: any) => boolean) {
        const deadline = Date.now() + 15000;
        while (Date.now() < deadline) {
          const r = await api("/api/v2/debug/" + debugId + "?after=" + cursor);
          cursor = r.next;
          if (r.messages.some(check)) return;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        throw Error("调试器准备超时");
      }
      await send("initialize", {
        adapterID: "debugpy",
        clientID: "harness",
        linesStartAt1: true,
        columnsStartAt1: true,
        pathFormat: "path",
        supportsRunInTerminalRequest: false,
      });
      await until((m) => m.type === "response" && m.command === "initialize");
      await send("launch", {
        program: file,
        justMyCode: true,
        stopOnEntry: false,
      });
      await until((m) => m.event === "initialized");
      const bp = await send("setBreakpoints", {
        source: { path: file },
        breakpoints: [{ line }],
        lines: [line],
      });
      await until((m) => m.type === "response" && m.request_seq === bp.seq);
      await send("configurationDone", {});
      setMessages([]);
      setFrames([]);
      setVariables([]);
      setPhase("运行中");
      setId(debugId);
    } catch (e) {
      onError(e);
    }
  }
  return (
    <div className="debug-pane">
      <div className="tool-title">
        <b>Python 调试 · {phase}</b>
        <select
          aria-label="恢复调试会话"
          value={id}
          onChange={(e) => setId(e.target.value)}
        >
          <option value="">恢复已有调试</option>
          {sessions.map((d) => (
            <option key={d.id} value={d.id}>
              {d.id.slice(0, 8)} · {d.alive ? "已连接" : "已结束"}
            </option>
          ))}
        </select>
        <span>
          {file || "先打开 .py 文件"}:{line}
        </span>
        <button disabled={!!blockedReason||!workspace.capabilities.debug} onClick={start}>
          在此行断点启动
        </button>
        <button
          disabled={!id}
          onClick={() =>
            command("continue", { threadId: thread.current }).catch(onError)
          }
        >
          继续
        </button>
        <button
          disabled={!stopped}
          onClick={() =>
            command("next", { threadId: thread.current }).catch(onError)
          }
        >
          单步
        </button>
        <button
          disabled={!id}
          onClick={() =>
            command("disconnect", { terminateDebuggee: true }).catch(onError)
          }
        >
          结束
        </button>
      </div>
      <div className="debug-columns">
        <div>
          <h4>调用栈</h4>
          {frames.map((frame) => (
            <button
              key={frame.id}
              onClick={() => {
                const path = frame.source?.path || "";
                onOpen(
                  path.startsWith(workspace.path)
                    ? path
                        .slice(workspace.path.length + 1)
                        .replaceAll("\\", "/")
                    : file,
                  frame.line,
                );
                ((frameId.current = frame.id),
                command("scopes", { frameId: frame.id })).catch(onError);
              }}
            >
              {frame.name}:{frame.line}
            </button>
          ))}
          <h4>变量</h4>
          {variables.map((v) => (
            <div key={(v.group || "watch") + v.name}>
              <button
                onClick={() =>
                  v.variablesReference &&
                  command("variables", {
                    variablesReference: v.variablesReference,
                  }).catch(onError)
                }
              >
                {v.variablesReference ? "▸ " : ""}
                {v.group ? v.group + " · " : ""}
                {v.name} = {v.value}
              </button>
            </div>
          ))}
        </div>
        <pre>
          {messages
            .filter(
              (m) =>
                m.event === "output" ||
                (m.type === "response" && !m.success) ||
                m.event === "terminated",
            )
            .map((m) => m.body?.output || m.message || m.event)
            .join("")}
        </pre>
      </div>
      <div className="run-config">
        <input
          aria-label="调试表达式"
          placeholder="暂停时查看表达式"
          value={watch}
          onChange={(e) => setWatch(e.target.value)}
        />
        <button
          disabled={!stopped || !watch}
          onClick={() =>
            command("evaluate", {
              expression: watch,
              frameId: frameId.current,
              context: "watch",
            }).catch(onError)
          }
        >
          求值
        </button>
        <small>表达式由调试目标执行，可能产生副作用。</small>
      </div>
      {blockedReason && <p role="status">{blockedReason}</p>}
      {!blockedReason && !workspace.capabilities.debug && (
        <p>后端未安装 debugpy。可在外部 IDE 中调试，或安装工作台调试依赖。</p>
      )}
    </div>
  );
}
