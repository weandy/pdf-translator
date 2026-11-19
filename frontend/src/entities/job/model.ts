import { queryOptions, useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { watchJob, type JobSnapshot } from "@/entities/job/sync";
import { api, ApiError, type Job } from "@/shared/api/client";
import { readJSON, writeJSON } from "@/shared/storage/local";

export const STAGE_LABELS: Record<string, string> = {
  parse: "版面解析",
  fuse: "对齐文字",
  segment: "切分段落",
  glossary: "抽取术语",
  translate: "翻译",
  render: "生成背景页",
  done: "完成",
};

export const STAGE_ORDER = [
  "parse",
  "fuse",
  "segment",
  "glossary",
  "translate",
  "render",
];

/**
 * “我的任务”：服务器不提供任务列表（公开部署时不能让人看到别人的任务），
 * 本浏览器创建过的任务 ID 保存在 localStorage 里。
 */
const MY_JOBS_KEY = "pdftr.jobs.v1";
const RESTORED_JOBS_KEY = "pdftr.jobs.restored.v1";
const MY_JOBS_MAX = 30;
const isJobId = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{16}$/.test(value);

function readJobIds(key: string): string[] {
  const ids = readJSON<unknown>(key, []);
  return Array.isArray(ids) ? ids.filter(isJobId) : [];
}

export function myJobIds(): string[] {
  return [...new Set([...readJobIds(MY_JOBS_KEY), ...readJobIds(RESTORED_JOBS_KEY)])];
}

export function rememberJob(id: string): void {
  if (!isJobId(id)) return;
  writeJSON(MY_JOBS_KEY, [id, ...readJobIds(MY_JOBS_KEY).filter((x) => x !== id)].slice(0, MY_JOBS_MAX));
}

/** 移出批次的任务继续可在首页查看，后续上传也不会挤出这组任务。 */
export function restoreMyJobs(ids: string[]): void {
  writeJSON(RESTORED_JOBS_KEY, [...new Set([...ids.filter(isJobId), ...readJobIds(RESTORED_JOBS_KEY)])]);
}

export function forgetJob(id: string): void {
  for (const key of [MY_JOBS_KEY, RESTORED_JOBS_KEY]) {
    const ids = readJobIds(key);
    if (ids.includes(id))
      writeJSON(
        key,
        ids.filter((x) => x !== id),
      );
  }
}

export const MY_JOBS_QUERY = ["my-jobs"];

export interface MyJobsResult {
  jobs: Job[];
  unavailableIds: string[];
  failedIds: string[];
  warning: string | null;
}

function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 410);
}

export function useMyJobs(additionalIds: string[] = []): { data: MyJobsResult; isPending: boolean } {
  const queryClient = useQueryClient();
  const ids = [...new Set([...myJobIds(), ...additionalIds.filter(isJobId)])];
  const results = useQueries({
    // 独立查询，让一个慢请求或网络错误不影响其他论文的状态和下载。
    queries: ids.map((id) =>
      queryOptions({
        queryKey: [...MY_JOBS_QUERY, id],
        queryFn: async ({ signal }): Promise<Job | null> => {
          try {
            return await api.job(id, signal);
          } catch (error) {
            if (!isUnavailable(error) || signal.aborted) throw error;
            forgetJob(id);
            return null;
          }
        },
        retry: false,
        staleTime: (query) => (query.state.data === null ? "static" : 0),
        refetchInterval: (query) => {
          if (query.state.status === "error") return 3000;
          const status = query.state.data?.status;
          return status === "queued" || status === "running" ? 3000 : false;
        },
      }),
    ),
  });

  // 服务端确认的 404/410 仍可用于历史展示，即使任务已从本机列表清理。
  const unavailableIds = queryClient
    .getQueriesData<Job | null>({ queryKey: MY_JOBS_QUERY, predicate: (query) => query.state.data === null })
    .map(([key]) => String(key[1]));
  const jobs: Job[] = [];
  const failedIds: string[] = [];
  results.forEach((result, index) => {
    const id = ids[index];
    if (result.isError && id) failedIds.push(id);
    else if (result.data) jobs.push(result.data);
  });
  return {
    data: {
      jobs,
      unavailableIds,
      failedIds,
      warning: failedIds.length ? `${failedIds.length} 个任务暂时无法读取，正在重试。` : null,
    },
    isPending: results.some((result) => result.isPending),
  };
}

export async function deleteJob(id: string): Promise<void> {
  try {
    await api.deleteJob(id);
  } catch (e) {
    if (!isUnavailable(e)) throw e;
  }
  forgetJob(id);
}

/** 任务实时状态：SSE 推送与定期校准并行，漏掉完成事件也能自动恢复。 */
export function useJob(id: string): {
  job: Job | null;
  error: string | null;
  refresh: () => void;
} {
  const [snapshot, setSnapshot] = useState<JobSnapshot & { id: string }>({ id, job: null, error: null });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    return watchJob(id, (next) =>
      setSnapshot((previous) => ({
        id,
        ...next,
        // 编辑后刷新摘要时保留已打开的阅读器；切换任务时不展示上一份文档。
        job: next.job ?? (previous.id === id ? previous.job : null),
      })),
    );
  }, [id, tick]);

  return {
    job: snapshot.id === id ? snapshot.job : null,
    error: snapshot.id === id ? snapshot.error : null,
    refresh: () => setTick((t) => t + 1),
  };
}
