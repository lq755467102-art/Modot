// 从工作间操作或日志中列出已定位的问题，并交给编辑器跳转。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./client";
import type { ProcessRun } from "./domain";
import {logLocations} from './logLocations';
import './problems-navigation.css';
/** Heuristic locations from real output. These are navigation aids, not test verdicts. */
export function ProblemsPane({
  run,
  onOpen,
  onError,
  visible:shown=true,
}: {
  run: ProcessRun | null;
  onOpen: (p: string, l: number) => void;
  onError: (e: unknown) => void;
  visible?:boolean;
}) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState("");
  const [filter,setFilter]=useState('');
  const active=useRef(shown);active.current=shown;
  useEffect(()=>setFilter(''),[run?.id]);
  useEffect(() => {
    setText("");
    setNotice("");
    if (!run) return;
    let cursor = 0,
      cancelled = false,
      timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if(!active.current){timer=setTimeout(poll,500);return;}
      try {
        const r = await api(
          "/api/v2/processes/" + run!.id + "?after=" + cursor,
        );
        if (cancelled) return;
        cursor = r.next;
        if (r.outputNotice) setNotice(r.outputNotice);
        setText((old) => (old + r.text).slice(-500000));
        if (r.hasMore || r.process.state === "running")
          timer = setTimeout(poll, r.hasMore ? 10 : 1000);
      } catch (e) {
        if (!cancelled) onError(e);
      }
    }
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [run?.id]);
  const rows=useMemo(()=>text.split(/\r?\n/).flatMap((line,index)=>logLocations(line).map(location=>({...location,index}))),[text]);
  const needle=filter.trim().toLocaleLowerCase();
  const visible=rows.filter(row=>!needle||`${row.path} ${row.line} ${row.text}`.toLocaleLowerCase().includes(needle));
  return (
    <div className="problems-pane">
      {notice && <p role="status">{notice}</p>}
      <p>
        从所选运行的真实输出识别到 {rows.length}{" "}
        个代码位置。测试是否通过以退出码及原始日志为准。
      </p>
      {run&&<><label className="problem-filter">筛选代码位置<input aria-label="筛选代码位置" value={filter} onChange={event=>setFilter(event.target.value)} placeholder="文件名或输出文字"/></label>
        <p role="status">当前匹配 {visible.length} 个位置</p>
        {!text&&<p>当前尚无已读取的输出。</p>}
        {!!text&&!rows.length&&<p>近期输出中未识别到代码位置，可查看原始输出。</p>}
        {!!rows.length&&!visible.length&&<p>没有匹配的代码位置。可以清除筛选条件。</p>}
      </>}
      {visible.map((r) => (
        <button className="problem-location" key={r.index+':'+r.start} onClick={() => onOpen(r.path, r.line)}>
          {r.path}:{r.line}
          <small>{r.text}</small>
        </button>
      ))}
      {!run && <p>先在右侧运行选择器中选择一条检查记录。</p>}
      {!!text && (
        <details>
          <summary>近期原始输出</summary>
          <pre>{text}</pre>
        </details>
      )}
    </div>
  );
}
