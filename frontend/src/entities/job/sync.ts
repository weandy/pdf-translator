import { api, ApiError, subscribeJob, type Job } from "@/shared/api/client";

export interface JobSnapshot {
  job: Job | null;
  error: string | null;
}

const POLL_INTERVAL = 3000;
const REQUEST_TIMEOUT = 10000;
const MAX_RETRY_DELAY = 30000;
const isTerminal = (job: Job) => job.status === "done" || job.status === "failed" || job.status === "cancelled";

/** SSE 负责即时更新，定期 GET 校准，避免长连接正常但漏掉完成事件时一直等待。 */
export function watchJob(id: string, onChange: (snapshot: JobSnapshot) => void): () => void {
  let stopped = false;
  let current: Job | null = null;
  let eventVersion = 0;
  let failures = 0;
  let inFlight = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let requestTimer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let unsubscribe = () => {};

  const stop = () => {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(requestTimer);
    controller?.abort();
    unsubscribe();
    document.removeEventListener("visibilitychange", resume);
    window.removeEventListener("online", resume);
  };

  const accept = (next: Job) => {
    if (stopped || next.id !== id) return;
    current = next;
    failures = 0;
    onChange({ job: next, error: null });
    if (isTerminal(next)) stop();
  };

  const schedule = (delay: number) => {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(() => void poll(), delay);
  };

  const poll = async () => {
    if (stopped || inFlight) return;
    inFlight = true;
    const version = eventVersion;
    const requestController = new AbortController();
    controller = requestController;
    try {
      const timeout = new Promise<never>((_resolve, reject) => {
        requestTimer = setTimeout(() => {
          requestController.abort();
          reject(new Error("任务状态请求超时，正在重试"));
        }, REQUEST_TIMEOUT);
      });
      const next = await Promise.race([api.job(id, requestController.signal), timeout]);
      // GET 发出后如果收到了更新的推送，丢弃这次可能过期的快照，防止状态倒退。
      if (!stopped && version === eventVersion) accept(next);
    } catch (error) {
      if (stopped) return;
      failures += 1;
      const notFound = error instanceof ApiError && error.status === 404;
      // 有有效进度时继续展示并后台恢复，短暂断网不应把整个任务替换成错误页。
      if (!current || notFound) onChange({ job: current, error: String(error) });
      if (notFound) stop();
    } finally {
      clearTimeout(requestTimer);
      inFlight = false;
      if (!stopped) schedule(Math.min(POLL_INTERVAL * 2 ** Math.min(Math.max(failures - 1, 0), 4), MAX_RETRY_DELAY));
    }
  };

  function resume() {
    if (stopped || document.visibilityState === "hidden") return;
    failures = 0;
    clearTimeout(timer);
    void poll();
  }

  onChange({ job: null, error: null });
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("online", resume);
  try {
    unsubscribe = subscribeJob(
      id,
      (next) => {
        if (stopped) return;
        eventVersion += 1;
        accept(next);
      },
      () => {
        if (!stopped) {
          clearTimeout(timer);
          void poll();
        }
      },
    );
    if (stopped) unsubscribe();
  } catch {
    // EventSource 不可用时，独立的 GET 仍能打开任务并持续更新。
  }
  void poll();
  return stop;
}
