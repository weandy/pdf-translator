/**
 * 后端接口客户端。类型来自后端 OpenAPI 生成的 schema.d.ts（npm run gen:api）。
 *
 * Key 保存在用户浏览器里，由 app 层通过 setAuthHeaders 注入（shared 层不依赖上层的设置模块）。
 */
import type { components } from "@/shared/api/schema";

export type Job = components["schemas"]["JobOut"];
export type JobOptions = components["schemas"]["JobOptions"];
export type DocumentData = components["schemas"]["DocumentOut"];
export type PageData = components["schemas"]["PageOut"];
export type BlockData = components["schemas"]["BlockOut"];
export type SegmentData = components["schemas"]["SegmentOut"];
export type ServerConfig = components["schemas"]["ConfigOut"];
export type Estimate = components["schemas"]["Estimate"];
export type Quality = "default" | "careful";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

let authHeaders: () => Record<string, string> = () => ({});

export function setAuthHeaders(provider: () => Record<string, string>): void {
  authHeaders = provider;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...authHeaders(), ...init.headers },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // 非 JSON 错误体
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  body: JSON.stringify(body),
  headers: { "Content-Type": "application/json" },
});

export interface Health {
  status: string;
  version: string;
  data_dir_writable: boolean;
}

export const api = {
  health: () => request<Health>("/api/health"),
  config: () => request<ServerConfig>("/api/config"),
  estimate: (pages: number, quality: Quality, model?: string) =>
    request<Estimate>(
      `/api/estimate?${new URLSearchParams({ pages: String(pages), quality, ...(model ? { model } : {}) })}`,
    ),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  deleteJob: (id: string) => request<void>(`/api/jobs/${id}`, { method: "DELETE" }),
  createJob: (file: File, options: Partial<JobOptions>) => {
    const form = new FormData();
    form.append("file", file);
    form.append("options", JSON.stringify(options));
    return request<Job>("/api/jobs", { method: "POST", body: form });
  },
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: "POST" }),
  document: (id: string) => request<DocumentData>(`/api/jobs/${id}/document`),
  editSegment: (id: string, target: string) =>
    request<SegmentData>(`/api/segments/${id}`, { method: "PATCH", ...json({ target }) }),
  retranslate: (id: string, quality: Quality) =>
    request<SegmentData>(`/api/segments/${id}/retranslate`, { method: "POST", ...json({ quality }) }),
};

export const fileUrl = (jobId: string, name: "original.pdf" | "background.pdf") => `/api/jobs/${jobId}/files/${name}`;
export type ExportMode = "mono" | "dual";
export const exportUrl = (jobId: string, mode: ExportMode) => `/api/jobs/${jobId}/export?mode=${mode}`;
export const formulaUrl = (segmentId: string, key: string) => `/api/segments/${segmentId}/formulas/${key}.png`;

/** 订阅任务进度（SSE）。返回取消订阅函数。 */
export function subscribeJob(id: string, onJob: (job: Job) => void, onError?: () => void): () => void {
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (e) => {
    const job = JSON.parse(e.data as string) as Job;
    onJob(job);
    if (job.status === "done" || job.status === "failed") es.close();
  };
  es.onerror = () => {
    es.close();
    onError?.();
  };
  return () => es.close();
}
