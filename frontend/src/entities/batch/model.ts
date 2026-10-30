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
