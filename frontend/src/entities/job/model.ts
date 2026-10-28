import { useEffect, useState } from "react";

import { api, subscribeJob, type Job } from "@/shared/api/client";

export const STAGE_LABELS: Record<string, string> = {
  parse: "版面解析",
  fuse: "对齐文字",
  segment: "切分段落",
  glossary: "抽取术语",
  translate: "翻译",
  render: "生成背景页",
  done: "完成",
};

export const STAGE_ORDER = ["parse", "fuse", "segment", "glossary", "translate", "render"];

/** 任务实时状态：先取一次，再订阅 SSE；连接断开时退回轮询。 */
export function useJob(id: string): { job: Job | null; error: string | null; refresh: () => void } {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unsubscribe = () => {};
    const poll = () => {
      api.job(id).then(
        (j) => {
          if (!alive) return;
          setJob(j);
          if (j.status === "queued" || j.status === "running") timer = setTimeout(poll, 3000);
        },
        (e: unknown) => alive && setError(String(e)),
      );
    };
    unsubscribe = subscribeJob(
      id,
      (j) => alive && setJob(j),
      () => alive && poll(),
    );
    return () => {
      alive = false;
      unsubscribe();
      clearTimeout(timer);
    };
  }, [id, tick]);

  return { job, error, refresh: () => setTick((t) => t + 1) };
}
