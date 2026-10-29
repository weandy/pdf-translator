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

/** 批量删除，返回删掉的词条（用于撤销）。 */
export function removeTerms(sources: string[]): TermEntry[] {
  const gone = new Set(sources.map((s) => s.trim().toLowerCase()));
  const removed = current.filter((t) => gone.has(t.source.toLowerCase()));
  commit(current.filter((t) => !gone.has(t.source.toLowerCase())));
  return removed;
}

/** 撤销删除：原样放回（保留原来的修改时间），已有同名词条的不覆盖。 */
export function restoreTerms(entries: TermEntry[]): void {
  const have = new Set(current.map((t) => t.source.toLowerCase()));
  commit([...current, ...entries.filter((e) => !have.has(e.source.toLowerCase()))].slice(0, MAX_TERMS));
}

export interface ImportPlan {
  fresh: IncomingTerm[]; // 新词条
  changed: { incoming: IncomingTerm; old: TermEntry }[]; // 已有，但译法或说明不同
  same: number; // 与已有的完全相同，跳过
}

/** 导入前把识别结果分成新词条、译法不同、完全相同三组；导入文件里重复的英文术语只算第一条。 */
export function planImport(items: IncomingTerm[], terms: TermEntry[] = current): ImportPlan {
  const plan: ImportPlan = { fresh: [], changed: [], same: 0 };
  const seen = new Set<string>();
  for (const item of items) {
    const key = item.source.trim().toLowerCase();
    if (!key || !item.target.trim() || seen.has(key)) continue;
    seen.add(key);
    const old = findTerm(item.source, terms);
    if (!old) plan.fresh.push(item);
    else if (old.target === item.target.trim() && (!item.note.trim() || old.note === item.note.trim())) plan.same++;
    else plan.changed.push({ incoming: item, old });
  }
  return plan;
}

export function findTerm(source: string, terms: TermEntry[] = current): TermEntry | undefined {
  return terms.find((t) => same(t.source, source));
}

/** 发给后端的 {英文: 中文}。 */
export function termMap(terms: TermEntry[] = current): Record<string, string> {
  return Object.fromEntries(terms.map((t) => [t.source, t.target]));
}

/** 导出为 TSV（从表格软件复制粘贴也是这个格式）。 */
export function toTsv(terms: TermEntry[] = current): string {
  const cell = (s: string) => s.replace(/[\t\n]+/g, " ");
  return [["英文", "中文", "说明"], ...terms.map((t) => [t.source, t.target, t.note])]
    .map((r) => r.map(cell).join("\t"))
    .join("\n");
}

/** 导出为 JSON：[{source, target, note}]，可以再导入。 */
export function toJson(terms: TermEntry[] = current): string {
  return JSON.stringify(
    terms.map(({ source, target, note }) => ({ source, target, note })),
    null,
    2,
  );
}

/** 导出为 CSV（Excel 打开不乱码：带 BOM）。 */
export function toCsv(terms: TermEntry[] = current): string {
  const cell = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const rows = [["英文", "中文", "说明"], ...terms.map((t) => [t.source, t.target, t.note])];
  return "﻿" + rows.map((r) => r.map(cell).join(",")).join("\n");
}
