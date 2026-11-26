// 把旧会话样式限定在新版会话视图内，供构建时生成虚拟样式表。
// 主要引用方包括 `vite.config.ts`。
import postcss from "postcss";

/** Reuse the existing conversation stylesheet without affecting engineering views. */
export function scopeConversationStyles(source: string) {
  const sheet = postcss.parse(source);
  sheet.walkRules(rule => {
    let parent = rule.parent;
    while (parent) {
      if (parent.type === "atrule" && /keyframes$/i.test(parent.name)) return;
      parent = parent.parent;
    }
    const selector = rule.selector
      .replace(/:root\b/g, ".legacy-conversation")
      .replace(/(?<![\w.#-])(?:html|body)(?![\w-])/g, ".legacy-conversation");
    // :is preserves selector lists and commas inside functional selectors.
    rule.selector = `:where(.legacy-conversation):is(${selector}), :where(.legacy-conversation) :is(${selector})`;
  });
  return sheet.toString();
}
