// 创建新版 React 渲染根和共用请求缓存，并加载桌面应用。
// 主要引用或加载入口包括 `desktop/renderer-v2/index.html`。
import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import "./style.css";
import "virtual:legacy-conversation.css";
import "./surface-navigation.css";
import "./code-review.css";
import "./settings.css";
import "../../renderer/fonts.css";
const client = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 1000,
      gcTime: 0,
    },
    mutations: { retry: false },
  },
});
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <App />
  </QueryClientProvider>,
);
