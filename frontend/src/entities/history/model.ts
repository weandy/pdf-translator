/**
 * 本机翻译历史：每次上传记下 {指纹, 文件名, 页数, 任务编号, 时间}，只存在这个浏览器里，不上传。
 * 服务器上的任务 24 小时后删除，历史仍保留，用来提醒“这篇翻译过”，并在首页列出已过期的翻译。
 */
import { useSyncExternalStore } from "react";

import { readJSON, remove, writeJSON } from "@/shared/storage/local";

export interface HistoryEntry {
  id: string; // 任务编号
  hash: string; // 文件指纹（SHA-256）
  filename: string;
  pages: number;
  createdAt: string;
}

const KEY = "pdftr.history.v1";
export const HISTORY_MAX = 200;

let current: HistoryEntry[] = readJSON<HistoryEntry[]>(KEY, []).filter(
  (e) => e && typeof e.id === "string" && typeof e.hash === "string",
);
const listeners = new Set<() => void>();

function commit(next: HistoryEntry[]): void {
  current = next;
  writeJSON(KEY, next);
  listeners.forEach((l) => l());
}

export function addHistory(entry: HistoryEntry): void {
  commit([entry, ...current.filter((e) => e.id !== entry.id)].slice(0, HISTORY_MAX));
}

/** 同一文件以前的翻译，最近的在前。 */
export function findByHash(hash: string): HistoryEntry[] {
  return current.filter((e) => e.hash === hash);
}

export function removeHistory(id: string): void {
  commit(current.filter((e) => e.id !== id));
}

export function clearHistory(): void {
  commit([]);
  remove(KEY);
}

export function useHistory(): HistoryEntry[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}
