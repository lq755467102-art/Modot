// readEditorVsix 导入原始包，editorSnippetCompletions 用相同的规则在 CodeMirror 中展开片段。
export type Snippet = {
  name: string;
  prefixes: string[];
  body: string;
  languages: string[];
  description: string;
};
// id 表示发布者和包名，digest 区分同名同版本的不同原包。snippets 与 skipped 对应预览中的两个计数。
export type EditorPack = {
  id: string;
  name: string;
  version: string;
  engine: string;
  digest: string;
  enabled: boolean;
  snippets: Snippet[];
  skipped: number;
  icon?: string;
};

const languages: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascriptreact',
  ts: 'typescript',
  mts: 'typescript',
  cts: 'typescript',
  tsx: 'typescriptreact',
  py: 'python',
  pyw: 'python',
  json: 'json',
  jsonc: 'jsonc',
  md: 'markdown',
  markdown: 'markdown',
  html: 'html',
  htm: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  vue: 'vue',
  svelte: 'svelte',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  xml: 'xml',
  svg: 'xml',
  rs: 'rust',
  go: 'go',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  java: 'java',
  kt: 'kotlin',
  sh: 'shellscript',
  bash: 'shellscript',
  ps1: 'powershell',
  rb: 'ruby',
  php: 'php',
  sql: 'sql',
};
// 工作间传入相对文件路径。未知扩展名归入纯文本，只能匹配纯文本或通用片段。
export function documentLanguage(path: string) {
  const file = path.split(/[\\/]/).pop()!.toLowerCase();
  if (file === 'dockerfile') return 'dockerfile';
  if (file === 'makefile') return 'makefile';
  const extension = file.split('.').pop()!;
  return Object.hasOwn(languages, extension) ? languages[extension] : 'plaintext';
}

// CodeMirror 使用带花括号的制表位。先切分字面文本，防止模板里的普通花括号被当成指令。
type Part = { text: string } | { field: number } | { variable: string };
const variables = new Set([
  'TM_FILENAME',
  'TM_FILENAME_BASE',
  'TM_SELECTED_TEXT',
  'TM_CURRENT_LINE',
  'TM_LINE_NUMBER',
  'TM_LINE_INDEX',
]);
// 安装预览用空变量检查语法，真正插入时用当前文件与选区填值。返回 undefined 的片段会计入跳过数。
export function snippetTemplate(body: string, values: Record<string, string> = {}): string | undefined {
  if (!body || body.length > 8192) return undefined;
  const parts: Part[] = [],
    defaults = new Map<number, string>();
  let literal = '',
    pos = 0,
    fields = 0;
  function flush() {
    if (literal) {
      parts.push({ text: literal });
      literal = '';
    }
  }
  while (pos < body.length) {
    const char = body[pos++];
    if (char === '\\' && pos < body.length && /[\\$}]/.test(body[pos])) {
      literal += body[pos++];
      continue;
    }
    if (char !== '$') {
      literal += char;
      continue;
    }
    const tail = body.slice(pos);
    const field = /^(?:\{(\d{1,3})(?::([^{}$\\]*))?\}|(\d{1,3})(?!\d))/.exec(tail);
    if (field) {
      if (++fields > 100) return undefined;
      flush();
      const index = Number(field[1] ?? field[3]);
      if (field[2] !== undefined && !defaults.has(index)) defaults.set(index, field[2]);
      parts.push({ field: index });
      pos += field[0].length;
      continue;
    }
    const variable = /^(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))/.exec(tail);
    if (variable) {
      const key = variable[1] ?? variable[2];
      if (!variables.has(key)) return undefined;
      flush();
      parts.push({ variable: key });
      pos += variable[0].length;
      continue;
    }
    if (/^[{\d]/.test(tail)) return undefined;
    literal += '$';
  }
  flush();
  if (fields && !parts.some((part) => 'field' in part && part.field === 0)) parts.push({ field: 0 });
  const escapeText = (text: string) => text.replace(/[{}]/g, (brace) => '\\' + brace);
  return parts
    .map((part) => {
      if ('text' in part) return escapeText(part.text);
      if ('variable' in part) return escapeText(values[part.variable] ?? '');
      const initial = defaults.get(part.field);
      return '$' + '{' + part.field + (initial ? ':' + initial : '') + '}';
    })
    .join('');
}
