import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    // 开发时把 /api 转发给本机后端（uvicorn 默认 8000 端口，可用 PDFTR_API 覆盖）
    proxy: { "/api": process.env.PDFTR_API ?? "http://127.0.0.1:8000" },
  },
  preview: {
    proxy: { "/api": process.env.PDFTR_API ?? "http://127.0.0.1:8000" },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
