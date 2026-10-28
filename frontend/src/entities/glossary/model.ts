/**
 * 用户术语库：只保存在当前浏览器，新建任务与单段重翻时随请求发给后端（后端不保存）。
 * 同一英文术语不分大小写只有一条；说明（note）只给自己看，不发给翻译服务。
 */
import { useSyncExternalStore } from "react";

import { readJSON, writeJSON } from "@/shared/storage/local";

export interface TermEntry {
  source: string;
  target: string;
  note: string;
  updatedAt: string;
}

export type IncomingTerm = Omit<TermEntry, "updatedAt">;

const KEY = "pdftr.glossary.v1";
export const MAX_TERMS = 5000;

let current: TermEntry[] = readJSON<TermEntry[]>(KEY, []);
const listeners = new Set<() => void>();

function commit(next: TermEntry[]): void {
  current = next;
  writeJSON(KEY, next);
  listeners.forEach((l) => l());
}

export function getTerms(): TermEntry[] {
  return current;
}

export function useTerms(): TermEntry[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}

const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/** 新增或按英文术语（不分大小写）覆盖；返回新增与更新的条数。超过上限的新词条不保存。 */
export function upsertTerms(items: IncomingTerm[], now = new Date().toISOString()): { added: number; updated: number } {
  const next = [...current];
  let added = 0;
  let updated = 0;
  for (const item of items) {
    const source = item.source.trim();
    const target = item.target.trim();
    if (!source || !target) continue;
    const i = next.findIndex((t) => same(t.source, source));
    const entry = { source, target, note: item.note.trim(), updatedAt: now };
    if (i >= 0) {
      next[i] = entry;
      updated++;
    } else if (next.length < MAX_TERMS) {
      next.push(entry);
      added++;
    }
  }
  commit(next);
  return { added, updated };
}

export function removeTerm(source: string): void {
  commit(current.filter((t) => !same(t.source, source)));
}

export function findTerm(source: string, terms: TermEntry[] = current): TermEntry | undefined {
  return terms.find((t) => same(t.source, source));
}

/** 发给后端的 {英文: 中文}。 */
export function termMap(terms: TermEntry[] = current): Record<string, string> {
  return Object.fromEntries(terms.map((t) => [t.source, t.target]));
}

/** 导出为 CSV（Excel 打开不乱码：带 BOM）。 */
export function toCsv(terms: TermEntry[] = current): string {
  const cell = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const rows = [["英文", "中文", "说明"], ...terms.map((t) => [t.source, t.target, t.note])];
  return "﻿" + rows.map((r) => r.map(cell).join(",")).join("\n");
}
