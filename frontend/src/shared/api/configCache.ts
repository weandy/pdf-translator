/**
 * 公开站点配置的本地 SWR 快照缓存。
 * 仅用于刷新首屏立即渲染通道切换面板以消除滞后与跳动；提交前仍必须等待实时 /api/config 返回确认。
 */
import { readJSON, remove, writeJSON } from "@/shared/storage/local";

import type { ServerConfig } from "./client";

const CONFIG_CACHE_KEY = "pdftr.public_config.v1";

function isCachedConfig(value: unknown): value is ServerConfig {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.version === "string" &&
    typeof candidate.max_pages === "number" &&
    typeof candidate.max_upload_mb === "number" &&
    typeof candidate.maintenance_mode === "boolean"
  );
}

export function readCachedConfig(): ServerConfig | undefined {
  const cached = readJSON<unknown>(CONFIG_CACHE_KEY, null);
  if (!isCachedConfig(cached)) return undefined;
  return { ...cached, admin_path: "" };
}

export function writeCachedConfig(config: ServerConfig): void {
  // 不将管理后台入口路径持久化到 localStorage，只缓存公开展示字段。
  writeJSON(CONFIG_CACHE_KEY, { ...config, admin_path: "" });
}

export function clearCachedConfig(): void {
  remove(CONFIG_CACHE_KEY);
}
