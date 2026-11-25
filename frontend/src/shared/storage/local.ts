/**
 * localStorage 安全读写。
 * 隐私模式、禁用站点数据等情况下 localStorage 可能不可用或抛异常，此时退回内存存储，页面照常工作。
 */

const memory = new Map<string, string>();

export type StorageResult = { ok: true } | { ok: false; error: string };

export function writeJSONChecked(key: string, value: unknown): StorageResult {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    memory.delete(key);
    return { ok: true };
  } catch {
    return { ok: false, error: "无法写入本地存储，请检查浏览器是否允许保存站点数据或存储空间是否充足。" };
  }
}

export function removeChecked(key: string): StorageResult {
  try {
    window.localStorage.removeItem(key);
    memory.delete(key);
    return { ok: true };
  } catch {
    return { ok: false, error: "无法清除本地存储，请检查浏览器是否允许修改站点数据。" };
  }
}

function storage(): Storage | null {
  try {
    const s = window.localStorage;
    const probe = "__pdftr_probe__";
    s.setItem(probe, "1");
    s.removeItem(probe);
    return s;
  } catch {
    return null;
  }
}

export function readJSON<T>(key: string, fallback: T): T {
  try {
    const raw = storage()?.getItem(key) ?? memory.get(key) ?? null;
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writeJSON(key: string, value: unknown): void {
  const raw = JSON.stringify(value);
  memory.set(key, raw);
  try {
    storage()?.setItem(key, raw);
  } catch {
    // 写入失败时只保留在内存里
  }
}

export function remove(key: string): void {
  memory.delete(key);
  try {
    storage()?.removeItem(key);
  } catch {
    // 忽略
  }
}
