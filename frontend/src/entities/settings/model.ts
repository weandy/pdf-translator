/**
 * 用户设置：翻译服务与 Key。只保存在当前浏览器（docs/01-tech-stack.md §4.4.2）。
 * “不记住 Key”时，Key 只在本次打开的页面内存中有效。
 */
import { useSyncExternalStore } from "react";

import type { Quality } from "@/shared/api/client";
import { readJSON, remove, writeJSON } from "@/shared/storage/local";

export type ParserId = "mineru-cloud" | "paddleocr-vl";

/** 解析服务的显示名称。 */
export const PARSER_NAMES: Record<ParserId, string> = { "mineru-cloud": "MinerU", "paddleocr-vl": "PaddleOCR" };

export interface Settings {
  baseUrl: string;
  model: string;
  llmKey: string;
  parserKey: string; // MinerU Token
  parser: ParserId; // 版面解析服务，默认 MinerU
  paddleKey: string; // PaddleOCR（星河社区）Token
  quality: Quality;
  includeReferences: boolean;
  translateFigures: boolean;
  translationSide: "left" | "right"; // 双语对照时译文在左边还是右边
  rememberKeys: boolean;
}

const KEY = "pdftr.settings.v1";

export const DEFAULTS: Settings = {
  baseUrl: "",
  model: "",
  llmKey: "",
  parserKey: "",
  parser: "mineru-cloud",
  paddleKey: "",
  quality: "default",
  includeReferences: false,
  translateFigures: true,
  translationSide: "right",
  rememberKeys: true,
};

let current: Settings = { ...DEFAULTS, ...readJSON<Partial<Settings>>(KEY, {}) };
const listeners = new Set<() => void>();

export function getSettings(): Settings {
  return current;
}

export function saveSettings(next: Settings): void {
  current = next;
  const persisted = next.rememberKeys ? next : { ...next, llmKey: "", parserKey: "", paddleKey: "" };
  writeJSON(KEY, persisted);
  listeners.forEach((l) => l());
}

export function clearKeys(): void {
  saveSettings({ ...current, llmKey: "", parserKey: "", paddleKey: "" });
  if (!current.rememberKeys) remove(KEY);
}

export function useSettings(): Settings {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}

/** 随请求发送的头：只发用户填写了的项，未填写的由服务端用部署者的共享配置。 */
export function authHeaders(s: Settings = current): Record<string, string> {
  const h: Record<string, string> = {};
  if (s.baseUrl) h["X-LLM-Base-URL"] = s.baseUrl;
  if (s.model) h["X-LLM-Model"] = s.model;
  if (s.llmKey) h["X-LLM-Key"] = s.llmKey;
  if (s.parserKey) h["X-Parser-Key"] = s.parserKey;
  if (s.paddleKey) h["X-Paddle-Key"] = s.paddleKey;
  return h;
}
