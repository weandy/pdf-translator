import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, type SiteStats } from "@/shared/api/client";
import { readJSON, writeJSON } from "@/shared/storage/local";

const VISITOR_KEY = "pdftr.stats.visitor.v1";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const STATS_QUERY = ["site-stats", 30] as const;

/** HTTP 自部署环境可能没有 randomUUID，getRandomValues 仍可生成匿名的随机标识。 */
function randomId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function visitorId(): string {
  const stored = readJSON<unknown>(VISITOR_KEY, null);
  if (typeof stored === "string" && UUID.test(stored)) return stored;
  const id = randomId();
  writeJSON(VISITOR_KEY, id);
  return id;
}

/** 只发送匿名标识，不发送路径、任务 ID 或任何用户设置；失败不影响主流程。 */
export function usePageVisit(navigationKey: string): void {
  const queryClient = useQueryClient();
  const last = useRef<{ key: string; eventId: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      if (last.current?.key !== navigationKey) last.current = { key: navigationKey, eventId: randomId() };
      const payload = { event_id: last.current.eventId, visitor_id: visitorId() };
      // StrictMode 可能重复运行 effect，复用 event_id 让服务端只计一次。
      void api
        .recordVisit(payload, controller.signal)
        .then(
          () => {
            if (!controller.signal.aborted) void queryClient.invalidateQueries({ queryKey: STATS_QUERY });
          },
          () => {},
        )
        .finally(() => clearTimeout(timer));
    } catch {
      clearTimeout(timer);
    }
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [navigationKey, queryClient]);
}

export function useSiteStats() {
  return useQuery<SiteStats>({
    queryKey: STATS_QUERY,
    queryFn: ({ signal }) => api.stats(30, signal),
    staleTime: 30000,
    retry: 1,
  });
}
