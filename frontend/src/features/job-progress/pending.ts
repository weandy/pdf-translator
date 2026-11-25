import { useEffect, useState } from "react";

import type { Job } from "@/shared/api/client";

export const PENDING_NOTICE_DELAY_MS = 25_000;

export function isMineruCloudQueued(message: string): boolean {
  const text = message.trim();
  if (/(正在上传|接收上传文件|waiting-file|免费通道排队|排队等待解析|正在解析|正在整理|超时|重试|重新提交)/i.test(text)) {
    return false;
  }
  return /mineru.*(?:等待解析资源|队列中|排队中|\bpending\b)/i.test(text);
}

type QueueJob = Pick<Job, "id" | "status" | "stage" | "options" | "message" | "parse_queue_elapsed_s">;

export function useMineruQueueElapsed(job: QueueJob): number {
  const serverElapsed = job.parse_queue_elapsed_s;
  const active = job.status === "running" && job.stage === "parse" && job.options.parser === "mineru-cloud";
  const queued = active && (serverElapsed === undefined ? isMineruCloudQueued(job.message) : serverElapsed !== null);
  // 旧服务端无计时字段时，从首次连续观察开始计时。
  const sample = serverElapsed === undefined ? job.id : job;
  const baseMs = (serverElapsed ?? 0) * 1000;
  const [observation, setObservation] = useState({ sample, queued });
  const [tick, setTick] = useState<{ observation: typeof observation; elapsed: number } | null>(null);
  if (observation.sample !== sample || observation.queued !== queued) {
    setObservation({ sample, queued });
  }

  useEffect(() => {
    if (!observation.queued) return;
    const monoMs = performance.now();
    let timer: ReturnType<typeof setTimeout>;
    const update = () => {
      const elapsed = baseMs + performance.now() - monoMs;
      if (elapsed >= PENDING_NOTICE_DELAY_MS) setTick({ observation, elapsed });
      timer = setTimeout(update, Math.max(1, elapsed < PENDING_NOTICE_DELAY_MS ? PENDING_NOTICE_DELAY_MS - elapsed : 1000));
    };
    timer = setTimeout(update, Math.max(1, PENDING_NOTICE_DELAY_MS - baseMs));
    return () => clearTimeout(timer);
  }, [baseMs, observation]);

  if (!queued) return 0;
  const elapsed = tick?.observation === observation ? tick.elapsed : baseMs;
  return elapsed >= PENDING_NOTICE_DELAY_MS ? elapsed : 0;
}
