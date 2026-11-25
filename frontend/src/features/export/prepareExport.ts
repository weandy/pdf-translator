import { api, ApiError, type ExportMode, type ExportPrepare } from "@/shared/api/client";

export interface ExportTarget {
  jobId: string;
  mode: ExportMode;
  side: "left" | "right";
}

export async function exportRequest<T>(
  request: (signal: AbortSignal) => Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  signal.throwIfAborted();
  const controller = new AbortController();
  const cancel = () => controller.abort(signal.reason);
  signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    return await request(controller.signal);
  } catch (error) {
    signal.throwIfAborted();
    if (controller.signal.aborted) throw new ApiError(408, "导出请求超时，请稍后重试");
    throw error;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", cancel);
  }
}

function pollDelay(signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, 1500);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export async function preparePdfExport(
  { jobId, mode, side }: ExportTarget,
  signal: AbortSignal,
): Promise<ExportPrepare & { snapshot: string }> {
  const start = () => exportRequest((requestSignal) => api.startExport(jobId, mode, side, requestSignal), signal);
  let result = await start();
  let restarted = false;
  for (;;) {
    signal.throwIfAborted();
    if (result.status === "ready" && result.filename && result.download_url && result.size_bytes != null) {
      return { ...result, filename: result.filename, download_url: result.download_url, size_bytes: result.size_bytes };
    }
    if (result.status === "expired" && !restarted) {
      restarted = true;
      result = await start();
      continue;
    }
    if (result.status !== "preparing") throw new Error(result.detail || "PDF 准备失效，请重新下载");
    await pollDelay(signal);
    result = await exportRequest(
      (requestSignal) => api.exportStatus(jobId, mode, side, result.snapshot, requestSignal),
      signal,
    );
  }
}

export function downloadNative(url: string, filename: string): void {
  const link = document.createElement("a");
  link.hidden = true;
  link.href = url;
  link.download = filename;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
  }
}
