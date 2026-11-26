// 定义新版桌面渲染的构建入口及旧会话样式虚拟模块。
import { defineConfig } from "vite";
import { readFileSync } from "node:fs";
import { scopeConversationStyles } from "./legacyStyles";
export default defineConfig({
  base: "./",
  worker:{format:"es"},
  plugins: [{
    name: "legacy-conversation-styles",
    resolveId(id) { if (id === "virtual:legacy-conversation.css") return id; },
    load(id) {
      if (id === "virtual:legacy-conversation.css")
        return scopeConversationStyles(readFileSync(new URL("../renderer/styles.css", import.meta.url), "utf8"));
    },
  }],
  build: { outDir: "dist", emptyOutDir: true },
});
