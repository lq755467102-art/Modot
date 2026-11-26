// 呈现拥有实际后端身份的终端进程和输出，并传递有界交互输入。
// 主要引用方包括 `Panels.tsx`。
import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api } from "./client";
import type { ProcessRun } from "./domain";
import {useApplicationSettings} from "./ApplicationSettings";
import {logLocations,terminalLocationRange} from './logLocations';
export function TerminalPane({
  run,
  onError,
  onAttach,
  onNavigate,
  visible=true,
  attachLabel='附到当前任务',
}: {
  run: ProcessRun;
  onError: (e: unknown) => void;
  onAttach?: (text: string) => void;
  onNavigate?: (path: string, line: number) => void;
  visible?:boolean;
  attachLabel?:string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState(run.state);
  const [stopping, setStopping] = useState(false);
  const [outputNotice, setOutputNotice] = useState("");
  const instance = useRef<Terminal | null>(null);
  const fitting = useRef<FitAddon | null>(null);
  const navigation=useRef(onNavigate);navigation.current=onNavigate;
  const shown=useRef(visible);shown.current=visible;
  const {theme,preferences}=useApplicationSettings();
  useEffect(() => {
    setOutputNotice("");
    const terminal = new Terminal({
      scrollback: 5000,
      convertEol: true,
    });
    const fit = new FitAddon();
    fitting.current=fit;
    terminal.loadAddon(fit);
    terminal.open(host.current!);
    if(shown.current)fit.fit();
    instance.current = terminal;
    const links = terminal.registerLinkProvider({
      provideLinks: (y, callback) => {
        const line=terminal.buffer.active.getLine(y-1);
        if(!line||!navigation.current){callback(undefined);return;}
        const text=line.translateToString();
        callback(logLocations(text).flatMap(location=>{
          const range=terminalLocationRange(line,location.start,location.end);
          return range?[{range:{start:{x:range.start,y},end:{x:range.end,y}},text:text.slice(location.start,location.end),activate:()=>navigation.current?.(location.path,location.line)}]:[];
        }));
      },
    });
    let ended = false,
      cursor = 0,
      timer: ReturnType<typeof setTimeout>;
    let inputQueue = Promise.resolve(),
      inputFailed = false;
    const input = terminal.onData((text) => {
      if (shown.current && run.kind === "terminal" && !inputFailed)
        inputQueue = inputQueue
          .then(() =>
            api("/api/v2/processes/" + run.id + "/input", { text }, true),
          )
          .then(
            () => {},
            (e) => {
              inputFailed = true;
              onError(e);
            },
          );
    });
    const observer = new ResizeObserver(() => {
      if(!shown.current)return;
      fit.fit();
      if (run.kind === "terminal")
        api("/api/v2/processes/" + run.id + "/resize", {
          rows: terminal.rows,
          cols: terminal.cols,
        }).catch(() => {});
    });
    observer.observe(host.current!);
    async function poll() {
      if(!shown.current){timer=setTimeout(poll,500);return;}
      try {
        const result = await api(
          "/api/v2/processes/" + run.id + "?after=" + cursor,
        );
        if (ended) return;
        terminal.write(result.text, () => {
          if (host.current)
            host.current.dataset.outputCursor = String(result.next);
        });
        cursor = result.next;
        setState(result.process.state);
        if (result.outputNotice) setOutputNotice(result.outputNotice);
        if (result.process.state === "running" || result.hasMore)
          timer = setTimeout(poll, 250);
      } catch (error) {
        if (!ended) {
          onError(error);
          timer = setTimeout(poll, 2000);
        }
      }
    }
    poll();
    return () => {
      ended = true;
      clearTimeout(timer);
      input.dispose();
      links.dispose();
      observer.disconnect();
      terminal.dispose();
      instance.current = null;
      fitting.current = null;
    };
  }, [run.id]);
  useEffect(()=>{if(visible)fitting.current?.fit();},[visible]);
  useEffect(()=>{
    const terminal=instance.current;
    if(!terminal)return;
    const palette=getComputedStyle(document.documentElement);
    terminal.options.theme={
      background:palette.getPropertyValue("--app-code-bg").trim(),
      foreground:palette.getPropertyValue("--app-ink").trim(),
      selectionBackground:palette.getPropertyValue("--app-selected").trim(),
      cursor:palette.getPropertyValue("--app-accent").trim(),
    };
    terminal.options.fontSize=preferences.codeSize??12;
    fitting.current?.fit();
  },[theme,preferences.codeSize,run.id]);
  function attach() {
    const t = instance.current;
    if (!t) return;
    const lines = [];
    for (
      let y = Math.max(0, t.buffer.active.length - 200);
      y < t.buffer.active.length;
      y++
    )
      lines.push(t.buffer.active.getLine(y)?.translateToString(true) || "");
    onAttach?.(
      "运行 " +
        run.id +
        " · 代码版本 " +
        run.revision +
        "\n" +
        lines.join("\n"),
    );
  }
  return (
    <div className="terminal-pane">
      <div className="tool-title">
        <span>
          {run.kind === "terminal" ? "交互终端" : "检查输出"} · {state}
        </span>
        <button disabled={!onAttach} onClick={attach}>
          {attachLabel}
        </button>
        {state === "running" && run.state === "running" && <button disabled={stopping} onClick={async () => {
          setStopping(true);
          try { await api("/api/v2/processes/" + run.id + "/stop", {}); }
          catch (error) { setStopping(false); onError(error); }
        }}>{stopping ? "正在结束…" : "结束进程"}</button>}
      </div>
      {outputNotice && <p role="status">{outputNotice}</p>}
      <div className="terminal-host" ref={host} />
    </div>
  );
}
