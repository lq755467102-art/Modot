// 将语言服务的补全、悬停和签名结果接到 CodeMirror。
// 主要引用方包括 `EditorView.tsx`。
import { EditorState, StateEffect, StateField, Prec } from '@codemirror/state';
import {
  EditorView,
  hoverTooltip,
  keymap,
  showTooltip,
  ViewPlugin,
  type Tooltip,
  type ViewUpdate,
} from '@codemirror/view';
import { isolateHistory } from '@codemirror/commands';
import { type CompletionContext, type Completion } from '@codemirror/autocomplete';
import type { LanguageSession } from './workroomLanguage';

type Hit = {
  name: string;
  kind: string;
  insert: string;
  source?: string;
  data?: unknown;
  description?: string;
  edits?: Detail['edits'];
  span?: { start: number; length: number };
};
type Detail = { signature: string; edits: { from: number; to: number; insert: string }[] };
type SymbolHover = {
  from?: number;
  to?: number;
  blocks: { code: boolean; text: string }[];
};
type Signature = {
  prefix: string;
  suffix: string;
  separator: string;
  parameters: string[];
  active: number;
  count: number;
};
const signature = StateEffect.define<Tooltip | null>();
const signatureField = StateField.define<Tooltip | null>({
  create: () => null,
  update(value, tr) {
    if (tr.docChanged || tr.selection) value = null;
    for (const e of tr.effects) if (e.is(signature)) value = e.value;
    return value;
  },
  provide: (field) => showTooltip.from(field),
});
function tip(pos: number, help: Signature): Tooltip {
  return {
    pos,
    above: true,
    create() {
      const dom = document.createElement('div');
      dom.className = 'workroom-signature';
      dom.setAttribute('role', 'status');
      dom.append(help.prefix);
      help.parameters.forEach((text, i) => {
        if (i) dom.append(help.separator);
        const el = document.createElement(i === help.active ? 'strong' : 'span');
        el.textContent = text;
        dom.append(el);
      });
      dom.append(help.suffix);
      return { dom };
    },
  };
}
export function semanticExtensions(session: LanguageSession, file: string) {
  const symbolHover = hoverTooltip(async (view, pos, side) => {
    const { doc } = view.state;
    const word = view.state.wordAt(pos);
    if (!word || (pos === word.from && side < 0) || (pos === word.to && side > 0)) return null;
    const offset = Math.min(pos, word.to - 1);
    const info = await session.query<SymbolHover>('hover', file, doc.toString(), offset).catch(() => null);
    if (!info?.blocks.length || !session.active() || !view.dom.isConnected || view.state.doc !== doc) return null;
    const from = info.from ?? word.from, to = info.to ?? word.to;
    if (from < 0 || from > offset || to <= offset || to > doc.length) return null;
    return {
      pos: from, end: to, above: true,
      create() {
        const dom = document.createElement('div');
        dom.className = 'workroom-symbol-hover';
        dom.setAttribute('role', 'tooltip');
        for (const block of info.blocks) {
          const part = document.createElement(block.code ? 'code' : 'div');
          part.textContent = block.text;
          dom.append(part);
        }
        return { dom };
      },
    };
  }, { hideOnChange: true });
  const complete = async (context: CompletionContext) => {
    const token = context.matchBefore(/[\p{L}\p{N}_$]+/u);
    if (
      !context.explicit &&
      !token &&
      !/(?:\.|::|->)$/.test(context.state.sliceDoc(Math.max(0, context.pos - 2), context.pos))
    )
      return null;
    const doc = context.state.doc,
      original = doc.toString(),
      pos = context.pos;
    const hits = await session.query<Hit[]>('complete', file, original, pos).catch(() => null);
    if (!hits?.length || context.aborted || !session.active()) return null;
    const from = token?.from ?? pos;
    const options: Completion[] = hits.map((hit) => {
      const details = () =>
        session.query<Detail>('detail', file, original, pos, {
          name: hit.name,
          source: hit.source,
          data: hit.data,
        });
      return {
        label: hit.name,
        detail: hit.source || undefined,
        type: hit.kind === 'method' ? 'method' : hit.kind === 'function' ? 'function' : 'variable',
        info: async () => {
          const el = document.createElement('span');
          el.textContent = hit.description || (await details().catch(() => null))?.signature || hit.name;
          return el;
        },
        apply(view, _item, start, end) {
          // A suggested import and its identifier are a single ordinary, undoable draft edit.
          if (!session.active() || view.state.doc !== doc || view.state.readOnly) return;
          const apply = (extra: Detail['edits']) => {
            if (
              !session.active() ||
              !view.dom.isConnected ||
              view.state.doc !== doc ||
              view.state.selection.main.head !== pos
            )
              return;
            const a = hit.span?.start ?? start,
              b = hit.span ? a + hit.span.length : end;
            const changes = [...extra, { from: a, to: b, insert: hit.insert }].sort(
              (x, y) => x.from - y.from || x.to - y.to,
            );
            if (
              changes.some(
                (x, i) =>
                  x.from < 0 ||
                  x.to < x.from ||
                  x.to > doc.length ||
                  (i > 0 && changes[i - 1].to > x.from),
              )
            )
              return;
            const change = view.state.changes(changes);
            view.dispatch({
              changes: change,
              selection: { anchor: change.mapPos(b, 1) },
              userEvent: 'input.complete',
              annotations: isolateHistory.of('full'),
            });
          };
          if (hit.edits) apply(hit.edits);
          else if (hit.source) {
            void details()
              .then((d) => {
                if (d) apply(d.edits);
              })
              .catch(() => {});
          } else apply([]);
        },
      };
    });
    return { from, options };
  };
  const hints = ViewPlugin.fromClass(
    class {
      timer?: ReturnType<typeof setTimeout>;
      next = 0;
      constructor(readonly view: EditorView) {}
      request() {
        clearTimeout(this.timer);
        const version = ++this.next;
        this.timer = setTimeout(async () => {
          const { doc, selection } = this.view.state,
            pos = selection.main.head;
          const value = await session
            .query<Signature>('signature', file, doc.toString(), pos)
            .catch(() => null);
          if (
            version !== this.next ||
            !session.active() ||
            !this.view.dom.isConnected ||
            doc !== this.view.state.doc ||
            pos !== this.view.state.selection.main.head
          )
            return;
          this.view.dispatch({ effects: signature.of(value ? tip(pos, value) : null) });
        }, 110);
      }
      update(u: ViewUpdate) {
        if (!u.docChanged && !u.selectionSet) return;
        const active = u.startState.field(signatureField, false);
        clearTimeout(this.timer);
        this.next++;
        const head = u.state.selection.main.head,
          last = u.state.sliceDoc(Math.max(0, head - 1), head);
        if ((u.docChanged && ['(', ','].includes(last)) || (active && last !== ')')) this.request();
      }
      destroy() {
        clearTimeout(this.timer);
        this.next++;
      }
    },
  );
  return [
    EditorState.languageData.of(() => [{ autocomplete: complete }]),
    symbolHover,
    signatureField,
    hints,
    Prec.high(
      keymap.of([
        {
          key: 'Mod-Shift-Space',
          run(view) {
            view.plugin(hints)?.request();
            return true;
          },
        },
        {
          key: 'Escape',
          run(view) {
            if (!view.state.field(signatureField, false)) return false;
            view.dispatch({ effects: signature.of(null) });
            return true;
          },
        },
      ]),
    ),
  ];
}
