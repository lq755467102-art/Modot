// 识别源码语言并生成供代码块使用的语法着色片段。
// 主要引用方包括 `CodeBlock.tsx` 和 `useSourceSyntax.ts`。
import { createHighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import type { ThemeRegistration,LanguageRegistration } from "shiki/types";
import lightTheme from "shiki/themes/github-light.mjs";
import darkTheme from "shiki/themes/github-dark.mjs";
const themes = {light: lightTheme, dark: darkTheme};

const loaders:Record<string,()=>Promise<{default:LanguageRegistration[]}>>={
  javascript:()=>import("shiki/langs/javascript.mjs"),typescript:()=>import("shiki/langs/typescript.mjs"),
  tsx:()=>import("shiki/langs/tsx.mjs"),jsx:()=>import("shiki/langs/jsx.mjs"),python:()=>import("shiki/langs/python.mjs"),
  json:()=>import("shiki/langs/json.mjs"),html:()=>import("shiki/langs/html.mjs"),css:()=>import("shiki/langs/css.mjs"),
  bash:()=>import("shiki/langs/bash.mjs"),yaml:()=>import("shiki/langs/yaml.mjs"),markdown:()=>import("shiki/langs/markdown.mjs"),
  rust:()=>import("shiki/langs/rust.mjs"),go:()=>import("shiki/langs/go.mjs"),sql:()=>import("shiki/langs/sql.mjs"),
  c:()=>import("shiki/langs/c.mjs"),java:()=>import("shiki/langs/java.mjs"),
};
const extensions:Record<string,string>={js:"javascript",mjs:"javascript",cjs:"javascript",ts:"typescript",tsx:"tsx",jsx:"jsx",py:"python",
  json:"json",html:"html",htm:"html",css:"css",sh:"bash",bash:"bash",yml:"yaml",yaml:"yaml",md:"markdown",rs:"rust",go:"go",sql:"sql",c:"c",h:"c",cpp:"c",hpp:"c",java:"java"};
// 每个片段带同一文本在明暗主题下的颜色和 Shiki 字体标记，可切换主题而无需再次分词。
export type SyntaxSpan={text:string;light:string;dark:string;lightFont:number;darkFont:number};
export type SyntaxLines=SyntaxSpan[][];
// Chat and editor use distinct Codex palettes but share the syntax engine.
const chatScopes=[
  ["comment","#666666","#aaaaaa"],
  ["keyword, storage","#ab4f7a","#f8a6c8"],
  ["constant","#ac4f23","#f1a275"],
  ["string","#3a843f","#83d197"],
  ["variable","#643cae","#b897f4"],
  ["entity.other.attribute-name","#b8802b","#f9dc78"],
  ["entity.name, support.function, support.type","#1f4e94","#63a8f8"],
];
const chatThemes:ThemeRegistration[]=[themes.light,themes.dark].map((theme,index)=>({
  ...theme,type:index===0?"light":"dark",name:theme.name+" Chat",
  tokenColors:chatScopes.map(([scope,light,dark])=>({scope,settings:{foreground:index===0?light:dark}})),
}));
let highlighter:ReturnType<typeof createHighlighterCore>|undefined;
// 仅从源码文件后缀选语法；缺少映射时上层可按普通文本呈现。
export function languageFor(path:string){return extensions[path.split('.').at(-1)?.toLowerCase()||''];}
// 对话代码按显式语言使用对话配色，保留源码按文件名选语法。
// 尺寸或行长超出时返回空，让调用方按原文呈现。
export async function tokenize(source:string,path:string,declared?:string):Promise<SyntaxLines|null> {
  const hint=declared?.toLowerCase()||"";
  const language=declared===undefined?languageFor(path):extensions[hint]||({shell:"bash",zsh:"bash"} as Record<string,string>)[hint]||hint;
  if(!Object.hasOwn(loaders,language)||source.length>131072||source.split('\n').some(line=>line.length>1000))return null;
  highlighter??=createHighlighterCore({themes:[themes.light as ThemeRegistration,themes.dark as ThemeRegistration,...chatThemes],langs:[],engine:createJavaScriptRegexEngine()});
  const instance=await highlighter;
  if(!instance.getLoadedLanguages().includes(language))await instance.loadLanguage((await loaders[language]()).default);
  const [light,dark]=declared===undefined?[themes.light,themes.dark]:chatThemes;
  const result=instance.codeToTokensWithThemes(source.replace(/\r\n/g,'\n'),{lang:language,themes:{light:light.name,dark:dark.name}});
  return result.map(line=>line.map(token=>({text:token.content,light:token.variants.light.color||'#0d0d0d',dark:token.variants.dark.color||'#fcfcfc',
    lightFont:token.variants.light.fontStyle||0,darkFont:token.variants.dark.fontStyle||0})));
}
