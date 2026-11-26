// The document keeps its real project URI so extensions can resolve project settings.
// Its Theia resource reads the supplied draft and has no save operation.
import * as monaco from '@theia/monaco-editor-core';
import { ResourceResolver } from '@theia/core/lib/common/resource.js';
import { CancellationToken } from '@theia/core/lib/common/cancellation.js';
import { StandaloneServices } from '@theia/monaco-editor-core/esm/vs/editor/standalone/browser/standaloneServices.js';
import { ILanguageFeaturesService } from '@theia/monaco-editor-core/esm/vs/editor/common/services/languageFeatures.js';

export function formattingDocuments(bind, sameFile) {
  let snapshot;
  bind(ResourceResolver).toConstantValue({
    priority: 1000,
    resolve(uri) {
      if (!snapshot || !sameFile(uri, snapshot.file)) throw Error('Different resource');
      const { draft } = snapshot;
      return { uri, readContents: async () => draft, dispose() {} };
    },
  });
  return async function format(args, openFile, until) {
    snapshot = args;
    try {
      // The lightweight host omits the full IDE's built-in language packs.
      if (
        args.language &&
        args.language !== 'plaintext' &&
        !monaco.languages.getLanguages().some((l) => l.id === args.language)
      )
        monaco.languages.register({
          id: args.language,
          filenames: [decodeURIComponent(new URL(args.file).pathname.split('/').pop())],
        });
      const widget = await openFile();
      const model = widget?.editor?.getControl()?.getModel();
      if (!model || !sameFile(model.uri, args.file) || model.getValue(undefined, true) !== args.draft)
        throw Error('宿主读取的文档与当前草稿不同，已取消预览。');
      if (args.language && args.language !== 'plaintext' && model.getLanguageId() !== args.language)
        monaco.editor.setModelLanguage(model, args.language);
      const providers = StandaloneServices.get(ILanguageFeaturesService).documentFormattingEditProvider;
      const id = args.extension.split('@')[0];
      const provider = await until(
        () => {
          const matches = providers.ordered(model).filter((p) => p.extensionId?.value?.toLowerCase() === id);
          if (matches.length > 1) throw Error('所选扩展为当前文件登记了多个文档格式化器。');
          return matches[0];
        },
        () => '所选扩展没有为 ' + model.getLanguageId() + ' 文档登记格式化器。',
      );
      const { tabSize, insertSpaces } = model.getOptions();
      const edits = await provider.provideDocumentFormattingEdits(
        model,
        { tabSize, insertSpaces },
        CancellationToken.None,
      );
      if (!Array.isArray(edits)) throw Error('扩展未返回格式化编辑。请检查文件语法和项目配置。');
      // VS Code ranges are UTF-16 offsets. Resolve all against the unchanged original model.
      const changes = edits
        .map(({ range, text, eol }) => {
          if (
            !range ||
            typeof text !== 'string' ||
            eol !== undefined ||
            !model.validateRange(range).equalsRange(range)
          )
            throw Error('格式化器返回了无效的编辑范围。');
          return {
            from: model.getOffsetAt({ lineNumber: range.startLineNumber, column: range.startColumn }),
            to: model.getOffsetAt({ lineNumber: range.endLineNumber, column: range.endColumn }),
            text,
          };
        })
        .sort((a, b) => a.from - b.from || a.to - b.to);
      const original = model.getValue();
      let end = 0;
      const parts = [args.draft.slice(0, args.draft.length - original.length)];
      for (const edit of changes) {
        if (edit.from < end) throw Error('格式化器返回的编辑范围重叠。');
        parts.push(original.slice(end, edit.from), edit.text);
        end = edit.to;
      }
      parts.push(original.slice(end));
      return { text: parts.join(''), provider: id, language: model.getLanguageId(), edits: changes.length };
    } finally {
      snapshot = undefined;
    }
  };
}
