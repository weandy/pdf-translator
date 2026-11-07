import { useQuery } from "@tanstack/react-query";
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
const MY_JOBS_MAX = 30;

export function myJobIds(): string[] {
  const ids = readJSON<unknown>(MY_JOBS_KEY, []);
  return Array.isArray(ids)
    ? ids.filter(
        (x): x is string => typeof x === "string" && /^[0-9a-f]{16}$/.test(x),
      )
    : [];
}

export function rememberJob(id: string): void {
  writeJSON(
    MY_JOBS_KEY,
    [id, ...myJobIds().filter((x) => x !== id)].slice(0, MY_JOBS_MAX),
  );
}

export function forgetJob(id: string): void {
  writeJSON(
    MY_JOBS_KEY,
    myJobIds().filter((x) => x !== id),
  );
}

export const MY_JOBS_QUERY = ["my-jobs"];

export function useMyJobs() {
  return useQuery({
    queryKey: MY_JOBS_QUERY,
    queryFn: async () => {
      const ids = myJobIds();
      const jobs = await Promise.all(
        ids.map((id) =>
          api
            .job(id)
            .catch((e: unknown) =>
              e instanceof ApiError && e.status === 404
                ? null
                : Promise.reject(e),
            ),
        ),
      );
      // 服务器上已过期删除的任务，从本地列表里去掉
      ids.forEach((id, i) => jobs[i] === null && forgetJob(id));
      return jobs.filter((j): j is Job => j !== null);
    },
    // 有任务在排队或进行时每 3 秒刷新一次（批量上传后留在首页看进度）
    refetchInterval: (q) =>
      q.state.data?.some((j) => j.status === "queued" || j.status === "running")
        ? 3000
        : false,
  });
}

export async function deleteJob(id: string): Promise<void> {
  try {
    await api.deleteJob(id);
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) throw e;
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
