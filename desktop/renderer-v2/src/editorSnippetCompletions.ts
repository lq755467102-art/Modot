// CodeMirror 从已启用的子插件取得补全。应用时再核对当前包，插入走普通编辑事务、撤销和草稿保存。
import { EditorState, Facet } from '@codemirror/state';
import { snippet, type Completion, type CompletionContext } from '@codemirror/autocomplete';
import { documentLanguage, snippetTemplate, type EditorPack } from './editorSnippets';
const activePacks = Facet.define<ReadonlyMap<string, string>, ReadonlyMap<string, string>>({
  combine: (values) => values[0] ?? new Map(),
});
export function editorSnippetExtensions(packs: readonly EditorPack[], path: string) {
  const language = documentLanguage(path),
    name = path.split(/[\\/]/).pop() ?? path;
  const enabled = packs.filter((p) => p.enabled);
  const options: Completion[] = [];
  for (const pack of enabled)
    for (const item of pack.snippets) {
      if (!item.languages.includes('*') && !item.languages.includes(language)) continue;
      for (const prefix of item.prefixes)
        options.push({
          label: prefix,
          type: 'snippet',
          detail: pack.name,
          info: item.description || item.name,
          apply(view, completion, from, to) {
            if (view.state.facet(activePacks).get(pack.id) !== pack.digest || view.state.readOnly) return;
            const line = view.state.doc.lineAt(from),
              selection = view.state.selection.main;
            const template = snippetTemplate(item.body, {
              TM_FILENAME: name,
              TM_FILENAME_BASE: name.replace(/\.[^.]+$/, ''),
              TM_CURRENT_LINE: line.text,
              TM_SELECTED_TEXT: view.state.sliceDoc(selection.from, selection.to),
              TM_LINE_NUMBER: String(line.number),
              TM_LINE_INDEX: String(line.number - 1),
            });
            if (template !== undefined) snippet(template)(view, completion, from, to);
          },
        });
    }
  if (!options.length) return [];
  function complete(context: CompletionContext) {
    const token = context.matchBefore(/[\w$-]+/);
    if (!token && !context.explicit) return null;
    return { from: token?.from ?? context.pos, options, validFor: /^[\w$-]*$/ };
  }
  return [
    activePacks.of(new Map(enabled.map((p) => [p.id, p.digest]))),
    EditorState.languageData.of(() => [{ autocomplete: complete }]),
  ];
}
