// 记事本式编辑入口。操作只作用于当前 CodeMirror 文档，保存仍由工作间按磁盘版本核对。
import { useEffect, useRef, useState } from 'react';
import type { EditorView } from '@codemirror/view';
import { keymap } from '@codemirror/view';
import { EditorState, Prec } from '@codemirror/state';
import { gotoLine, openSearchPanel } from '@codemirror/search';
import { undo, redo } from '@codemirror/commands';
import { startCompletion, nextSnippetField, prevSnippetField } from '@codemirror/autocomplete';
import { EditorExtensionManager } from './EditorExtensionManager';

// CodeMirror 的查找和替换共用面板，从“替换”进入时把焦点交给替换框。
function replacePanel(view: EditorView) {
  openSearchPanel(view);
  view.dom.querySelector<HTMLInputElement>('.cm-search input[name=replace]')?.focus();
  return true;
}
export const editingKeys = [
  Prec.high(
    keymap.of([
      { key: 'Mod-h', run: replacePanel },
      { key: 'Mod-g', run: gotoLine },
      { key: 'Tab', run: nextSnippetField, shift: prevSnippetField },
    ]),
  ),
  EditorState.phrases.of({
    Find: '查找',
    Replace: '替换',
    next: '下一个',
    previous: '上一个',
    all: '选择全部',
    'match case': '区分大小写',
    regexp: '正则表达式',
    'by word': '全字匹配',
    replace: '替换',
    'replace all': '全部替换',
    close: '关闭',
    'Go to line': '转到行',
    go: '转到',
    'current match': '当前匹配',
    'on line': '所在行',
  }),
];
export function EditorActions({
  view,
  wrap,
  onWrap,
}: {
  view: () => EditorView | undefined;
  wrap: boolean;
  onWrap: () => void;
}) {
  const [manage, setManage] = useState(false),
    menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (e: PointerEvent) => {
      if (menu.current?.open && !menu.current.contains(e.target as Node)) menu.current.open = false;
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, []);
  function run(command: (v: EditorView) => unknown) {
    const v = view();
    if (!v) return;
    if (menu.current) menu.current.open = false;
    v.focus();
    command(v);
  }
  return (
    <div className="workroom-edit-actions">
      <details
        ref={menu}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && menu.current?.open) {
            e.stopPropagation();
            menu.current.open = false;
            menu.current.querySelector('summary')?.focus();
          }
        }}
      >
        <summary>编辑</summary>
        <div className="workroom-edit-menu">
          <button onClick={() => run(undo)}>撤销</button>
          <button onClick={() => run(redo)}>重做</button>
          <button onClick={() => run(openSearchPanel)}>查找</button>
          <button onClick={() => run(replacePanel)}>替换</button>
          <button onClick={() => run(gotoLine)}>转到行</button>
          <button aria-pressed={wrap} onClick={() => run(onWrap)}>
            自动换行
          </button>
          <button onClick={() => run(startCompletion)}>插入代码片段</button>
          <button
            onClick={() => {
              if (menu.current) {
                menu.current.open = false;
                menu.current.querySelector('summary')?.focus();
              }
              setManage(true);
            }}
          >
            管理编辑器扩展…
          </button>
        </div>
      </details>
      {manage && <EditorExtensionManager onClose={() => setManage(false)} />}
    </div>
  );
}
