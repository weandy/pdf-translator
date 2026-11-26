import { readJSON, writeJSON } from "@/shared/storage/local";

export interface BatchEntry {
  id: string;
  name: string;
  jobIds: string[];
  createdAt: string;
}

export const BATCH_MAX = 30;
const KEY = "pdftr.batches.v1";

function valid(value: unknown): value is BatchEntry {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<BatchEntry>;
  return (
    typeof item.id === "string" &&
    typeof item.name === "string" &&
    typeof item.createdAt === "string" &&
    Array.isArray(item.jobIds) &&
    item.jobIds.every((id) => typeof id === "string")
  );
}

export function readBatches(): BatchEntry[] {
  const value = readJSON<unknown>(KEY, []);
  return Array.isArray(value) ? value.filter(valid) : [];
}

function writeBatches(entries: BatchEntry[]): void {
  writeJSON(KEY, entries.slice(0, BATCH_MAX));
}

export function rememberBatch(jobIds: string[], name?: string): BatchEntry | null {
  const ids = [...new Set(jobIds)].filter(Boolean);
  if (ids.length < 2) return null;
  const createdAt = new Date().toISOString();
  const entry: BatchEntry = {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    name: name?.trim() || `批次 ${new Date(createdAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`,
    jobIds: ids,
    createdAt,
  };
  writeBatches([entry, ...readBatches()]);
  return entry;
}

export function removeBatch(id: string): void {
  writeBatches(readBatches().filter((batch) => batch.id !== id));
}

export interface PruneBatchesResult {
  batches: BatchEntry[];
  promotedJobIds: string[];
  changed: boolean;
}

/** 清理已过期或已删除的批次成员：全组失效则移出分组，仅剩 1 篇时升格为单篇任务。 */
export function pruneBatches(unavailableIds: Iterable<string>): PruneBatchesResult {
  const unavailable = new Set(unavailableIds);
  const current = readBatches();
  if (unavailable.size === 0) return { batches: current, promotedJobIds: [], changed: false };
  const next: BatchEntry[] = [];
  const promotedJobIds: string[] = [];
  let changed = false;
  for (const batch of current) {
    const live = batch.jobIds.filter((id) => !unavailable.has(id));
    if (live.length === batch.jobIds.length) {
      next.push(batch);
      continue;
    }
    changed = true;
    if (live.length >= 2) {
      next.push({ ...batch, jobIds: live });
    } else if (live.length === 1 && live[0]) {
      promotedJobIds.push(live[0]);
    }
  }
  if (changed) writeBatches(next);
  return { batches: next, promotedJobIds, changed };
}
