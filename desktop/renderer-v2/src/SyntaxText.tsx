// 将语法着色后的文本片段渲染为界面文本节点。
// 主要引用方包括 `CodeBlock.tsx` 和 `SourceCodeView.tsx`。
import type { CSSProperties } from "react";
import type { SyntaxSpan } from "./syntaxEngine";

export function SyntaxText({text,tokens}:{text:string;tokens?:SyntaxSpan[]}) {
  if(!tokens||tokens.map(token=>token.text).join("")!==text)return text;
  return tokens.map((token,index)=><span className="source-syntax-token" key={index} style={{
    "--token-light":token.light,"--token-dark":token.dark,
    "--token-light-style":token.lightFont&1?"italic":"normal","--token-dark-style":token.darkFont&1?"italic":"normal",
    "--token-light-weight":token.lightFont&2?"bold":"normal","--token-dark-weight":token.darkFont&2?"bold":"normal",
    "--token-light-decoration":token.lightFont&4?"underline":"none","--token-dark-decoration":token.darkFont&4?"underline":"none",
  } as CSSProperties}>{token.text}</span>);
}
