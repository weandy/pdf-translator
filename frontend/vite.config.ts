import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

/**
 * pdf.js 解码扫描件常用的 CCITT 传真、JBIG2、JPEG 2000 图片要用单独的解码器，按 wasmUrl + 固定文件名加载。
 * 用纯 JS 版（不用改 CSP 放开 WASM），原名放在 /pdfjs/ 下：开发时直接提供，构建时原名输出（不能带哈希）。
 */
const PDFJS_DECODERS = ["jbig2_nowasm_fallback.js", "openjpeg_nowasm_fallback.js"];
const decoderPath = (name: string) => fileURLToPath(new URL(`./node_modules/pdfjs-dist/wasm/${name}`, import.meta.url));

function pdfjsDecoders(): Plugin {
  return {
    name: "pdfjs-decoders",
    configureServer(server) {
      server.middlewares.use("/pdfjs/", (req, res, next) => {
        const name = (req.url ?? "").replace(/^\//, "").split("?")[0] ?? "";
        if (!PDFJS_DECODERS.includes(name)) return next();
        res.setHeader("Content-Type", "text/javascript");
        res.end(readFileSync(decoderPath(name)));
      });
    },
    generateBundle() {
      for (const name of PDFJS_DECODERS) {
        this.emitFile({ type: "asset", fileName: `pdfjs/${name}`, source: readFileSync(decoderPath(name)) });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), pdfjsDecoders()],
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
