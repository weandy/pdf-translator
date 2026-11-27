/**
 * 后端接口客户端。类型来自后端 OpenAPI 生成的 schema.d.ts（npm run gen:api）。
 *
 * Key 保存在用户浏览器里，由 app 层通过 setAuthHeaders 注入（shared 层不依赖上层的设置模块）。
 */
import type { components } from "@/shared/api/schema";

export type Job = components["schemas"]["JobOut"];
export type Expansion = components["schemas"]["ExpansionOut"];
export type JobOptions = components["schemas"]["JobOptions"];
export type DocumentData = components["schemas"]["DocumentOut"];
export type PageData = components["schemas"]["PageOut"];
export type BlockData = components["schemas"]["BlockOut"];
export type RetranslateSegment = components["schemas"]["RetranslateSegmentOut"];
export type SegmentData = components["schemas"]["SegmentOut"];
export type LinkData = components["schemas"]["LinkOut"];
export type OutlineData = components["schemas"]["OutlineOut"];
export type ServerConfig = components["schemas"]["ConfigOut"];
export type FreeChannelPublic = components["schemas"]["FreeChannelPublicOut"];
export type LlmPreset = components["schemas"]["LlmPreset"];
export type TranslationMode = "byok" | "free";
export type Estimate = components["schemas"]["Estimate"];
export type Revision = components["schemas"]["RevisionOut"];
export type LlmCheck = components["schemas"]["LlmCheck"];
export type ParsedGlossary = components["schemas"]["ParsedGlossary"];
export type TermUse = components["schemas"]["TermUse"];
export type SiteStats = components["schemas"]["StatsOut"];
export type ExportPrepare = components["schemas"]["ExportPrepareOut"];
export type ExportStatus = components["schemas"]["ExportStatusOut"];
export type ExportBundleItem = components["schemas"]["BundleItemIn"];
export type ExportBundle = components["schemas"]["ExportBundleOut"];
export type Quality = "default" | "careful";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly staleIndexes?: number[],
  ) {
    super(message);
  }
}

let authHeaders: () => Record<string, string> = () => ({});

export function setAuthHeaders(provider: () => Record<string, string>): void {
  authHeaders = provider;
}

async function request<T>(path: string, init: RequestInit = {}, includeAuth = false): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(includeAuth ? authHeaders() : {}), ...init.headers },
  });
  if (!res.ok) {
    let staleIndexes: number[] | undefined;
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown; stale_indexes?: unknown };
      if (Array.isArray(body.stale_indexes) && body.stale_indexes.every((index) => Number.isSafeInteger(index))) {
        staleIndexes = body.stale_indexes as number[];
      }
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // 非 JSON 错误体
    }
    throw new ApiError(res.status, detail, staleIndexes);
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

export interface JobRequestOptions {
  mode?: TranslationMode;
  headers?: Record<string, string>;
}

function stripPersonalCredentialHeaders(headers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(headers).filter(([k]) => {
      const lower = k.toLowerCase();
      return lower !== "x-translation-mode" && !lower.startsWith("x-llm-") && !lower.startsWith("x-parser-") && !lower.startsWith("x-paddle-");
    }),
  );
}

function isJobRequestOptions(
  extra: JobRequestOptions | Record<string, string> | undefined,
): extra is JobRequestOptions {
  if (!extra) return false;
  return (
    ("mode" in extra && (extra.mode === undefined || extra.mode === "byok" || extra.mode === "free")) ||
    ("headers" in extra && (extra.headers === undefined || typeof extra.headers === "object"))
  );
}

async function documentRequest(id: string, signal?: AbortSignal): Promise<DocumentData> {
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  if (signal?.aborted) cancel();
  else signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  // 阅读数据不是翻译任务；及时结束等待，让用户能重试或下载已完成的结果。
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 30000);
  try {
    return await request<DocumentData>(`/api/jobs/${id}/document`, { signal: controller.signal });
  } catch (error) {
    if (timedOut) throw new ApiError(408, "读取译文超时，请稍后重新打开，或尝试下载 PDF。");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

export const api = {
  // 匿名统计无需模型凭据，避免将个人 Key 附带在统计请求中。
  stats: (days = 7, signal?: AbortSignal) =>
    request<SiteStats>(`/api/stats?days=${days}`, { signal, credentials: "omit" }, false),
  recordVisit: (body: components["schemas"]["StatsVisitIn"], signal?: AbortSignal) =>
    request<void>("/api/stats/visits", { method: "POST", ...json(body), signal, credentials: "omit" }, false),
  health: () => request<Health>("/api/health"),
  config: () => request<ServerConfig>("/api/config", { credentials: "omit", cache: "no-store" }, false),
  estimate: (pages: number, quality: Quality, model?: string) =>
    request<Estimate>(
      `/api/estimate?${new URLSearchParams({ pages: String(pages), quality, ...(model ? { model } : {}) })}`,
    ),
  job: (id: string, signal?: AbortSignal) => request<Job>(`/api/jobs/${id}`, { signal }),
  expandPages: (id: string, pages: number[] | null, mode: TranslationMode = "byok") =>
    request<Expansion>(
      `/api/jobs/${id}/expand-pages`,
      { method: "POST", ...json({ pages } satisfies components["schemas"]["ExpandPagesIn"]) },
      mode !== "free",
    ),
  expansionStatus: (id: string, opId: string, signal?: AbortSignal) =>
    request<Expansion>(`/api/jobs/${id}/expansions/${opId}`, { signal }),
  cancelExpansion: (id: string, opId: string) =>
    request<Expansion>(`/api/jobs/${id}/expansions/${opId}/cancel`, { method: "POST" }),
  deleteJob: (id: string) => request<void>(`/api/jobs/${id}`, { method: "DELETE" }),
  createJob: (
    file: File,
    options: Partial<JobOptions>,
    extra?: JobRequestOptions | Record<string, string>,
  ) => {
    const form = new FormData();
    form.append("file", file);
    form.append("options", JSON.stringify(options));
    const parsed = isJobRequestOptions(extra) ? extra : { headers: extra };
    const explicitHeaders = parsed.headers;
    const headerMode = new Headers(explicitHeaders).get("X-Translation-Mode")?.toLowerCase();
    const effectiveMode: TranslationMode | undefined =
      parsed.mode ?? (headerMode === "free" ? "free" : headerMode === "byok" ? "byok" : undefined);
    if (effectiveMode === "free") {
      // 免费体验模式绝不携带任何个人翻译或解析凭据头，避免泄露浏览器里的个人 Key/Token。
      const base = explicitHeaders ? stripPersonalCredentialHeaders(explicitHeaders) : {};
      return request<Job>(
        "/api/jobs",
        { method: "POST", body: form, headers: { ...base, "X-Translation-Mode": "free" } },
        false,
      );
    }
    if (explicitHeaders) {
      return request<Job>(
        "/api/jobs",
        {
          method: "POST",
          body: form,
          headers: effectiveMode ? { ...explicitHeaders, "X-Translation-Mode": effectiveMode } : explicitHeaders,
        },
        false,
      );
    }
    return request<Job>(
      "/api/jobs",
      {
        method: "POST",
        body: form,
        ...(effectiveMode ? { headers: { "X-Translation-Mode": effectiveMode } } : {}),
      },
      true,
    );
  },
  retryJob: (id: string, options?: { channelMode?: TranslationMode; mode?: TranslationMode }) => {
    const mode = options?.channelMode ?? options?.mode;
    return request<Job>(`/api/jobs/${id}/retry`, { method: "POST" }, mode !== "free");
  },
  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  document: documentRequest,
  editSegment: (id: string, target: string, signal?: AbortSignal, formulas?: Record<string, string>) =>
    request<SegmentData>(`/api/segments/${id}`, {
      method: "PATCH",
      ...json(formulas ? { target, formulas } : { target }),
      signal,
    }),
  /** userTerms：浏览器里最新的用户术语库 */
  retranslate: (
    id: string,
    quality: Quality,
    userTerms?: Record<string, string>,
    options?: { channelMode?: TranslationMode; mode?: TranslationMode; instruction?: string; signal?: AbortSignal },
  ) => {
    const mode = options?.channelMode ?? options?.mode;
    return request<RetranslateSegment>(
      `/api/segments/${id}/retranslate`,
      {
        method: "POST",
        ...json({ quality, user_terms: userTerms, instruction: options?.instruction }),
        signal: options?.signal,
      },
      mode !== "free",
    );
  },
  /** 一次重翻同一任务的多段（最多 20 段） */
  retranslateMany: (
    jobId: string,
    ids: string[],
    userTerms?: Record<string, string>,
    options?: { channelMode?: TranslationMode; mode?: TranslationMode },
  ) => {
    const mode = options?.channelMode ?? options?.mode;
    return request<SegmentData[]>(
      `/api/jobs/${jobId}/retranslate`,
      {
        method: "POST",
        ...json({ segment_ids: ids, quality: "default", user_terms: userTerms }),
      },
      mode !== "free",
    );
  },
  jobTerms: (jobId: string) => request<TermUse[]>(`/api/jobs/${jobId}/terms`),
  prepareExport: async (jobId: string, mode: ExportMode, side: "left" | "right" = "right") => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 310000);
    try {
      return await request<ExportPrepare>(`/api/jobs/${jobId}/export/prepare?mode=${mode}&side=${side}`, {
        method: "POST",
        signal: controller.signal,
      });
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError(408, "PDF 准备超时，请稍后重试");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  },
  startExport: (jobId: string, mode: ExportMode, side: "left" | "right", signal: AbortSignal) =>
    request<ExportStatus>(`/api/jobs/${jobId}/export/start?mode=${mode}&side=${side}`, { method: "POST", signal }),
  exportStatus: (jobId: string, mode: ExportMode, side: "left" | "right", snapshot: string, signal: AbortSignal) =>
    request<ExportStatus>(
      `/api/jobs/${jobId}/export/status?${new URLSearchParams({ mode, side, snapshot })}`, { signal },
    ),
  createExportBundle: (items: ExportBundleItem[], signal: AbortSignal) =>
    request<ExportBundle>("/api/exports/bundle", { method: "POST", ...json({ items }), signal }),
  parseGlossary: (file: File, ai: boolean) => {
    const form = new FormData();
    form.append("file", file);
    form.append("ai", String(ai));
    return request<ParsedGlossary>("/api/glossary/parse", { method: "POST", body: form }, ai);
  },
  /** 翻译服务的模型列表与连通测试；headers 用设置面板里尚未保存的值 */
  llmModels: async (headers: Record<string, string>, signal?: AbortSignal) => {
    const controller = new AbortController();
    const cancel = () => controller.abort(signal?.reason);
    if (signal?.aborted) cancel();
    else signal?.addEventListener("abort", cancel, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 20000);
    try {
      const result = await request<components["schemas"]["ModelsOut"]>(
        "/api/llm/models",
        { headers, signal: controller.signal },
        false,
      );
      return result.models;
    } catch (error) {
      if (timedOut) {
        throw new ApiError(408, "获取模型列表超时，请检查接口地址或稍后重试");
      }
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    }
  },
  llmCheck: (headers: Record<string, string>, signal?: AbortSignal) =>
    request<LlmCheck>("/api/llm/check", { method: "POST", headers, signal }, false),
  revisions: (jobId: string) => request<Revision[]>(`/api/jobs/${jobId}/revisions`),
  revert: (revisionId: number) =>
    request<SegmentData>(`/api/segments/revisions/${revisionId}/revert`, { method: "POST" }),
};

export const fileUrl = (jobId: string, name: "original.pdf" | "background.pdf", version?: number) =>
  `/api/jobs/${jobId}/files/${name}${name === "background.pdf" && version !== undefined ? `?v=${version}` : ""}`;
export type ExportMode = "mono" | "dual";
export const exportUrl = (jobId: string, mode: ExportMode, side: "left" | "right" = "right") =>
  `/api/jobs/${jobId}/export?mode=${mode}${mode === "dual" ? `&side=${side}` : ""}`;
export const formulaUrl = (segmentId: string, key: string) => `/api/segments/${segmentId}/formulas/${key}.png`;
export const blockImageUrl = (jobId: string, blockId: string, download = false) =>
  `/api/jobs/${jobId}/blocks/${encodeURIComponent(blockId)}.png${download ? "?download=true" : ""}`;

/** 订阅任务进度（SSE）。返回取消订阅函数。 */
export function subscribeJob(id: string, onJob: (job: Job) => void, onError?: () => void): () => void {
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (e) => {
    const job = JSON.parse(e.data as string) as Job;
    onJob(job);
    if ((job.status === "done" && !job.active_expansion) || job.status === "failed" || job.status === "cancelled") es.close();
  };
  es.onerror = () => {
    es.close();
    onError?.();
  };
  return () => es.close();
}
