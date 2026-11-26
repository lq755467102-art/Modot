// 定义桌面快捷键的默认值、按键组合解析及用户覆盖。
// 主要引用方包括 `App.tsx` 和 `Settings.tsx`。
export const DEFAULT_SHORTCUTS = {
  files: "Ctrl+P",
  commands: "Ctrl+Shift+P",
  terminal: "Ctrl+`",
  send: "Ctrl+Enter",
  save: "Ctrl+S",
  previous: "Alt+ArrowLeft",
  next: "Alt+ArrowRight",
};
export type ShortcutName = keyof typeof DEFAULT_SHORTCUTS;
export function chord(
  event: Pick<
    KeyboardEvent,
    "key" | "code" | "ctrlKey" | "shiftKey" | "altKey" | "metaKey"
  >,
) {
  if (["Control", "Alt", "Shift", "Meta", "Dead"].includes(event.key))
    return "";
  const key =
    event.code === "Backquote"
      ? "`"
      : event.key.length === 1
        ? event.key.toUpperCase()
        : event.key;
  return [
    event.ctrlKey && "Ctrl",
    event.altKey && "Alt",
    event.shiftKey && "Shift",
    event.metaKey && "Meta",
    key,
  ]
    .filter(Boolean)
    .join("+");
}
export function bound(
  name: ShortcutName,
  overrides?: Partial<typeof DEFAULT_SHORTCUTS>,
) {
  return overrides?.[name] || DEFAULT_SHORTCUTS[name];
}
