import { useEffect, useRef, useState } from "react";

import { api, ApiError, type ExportBundleItem } from "@/shared/api/client";
import { useToast } from "@/shared/feedback/Toast";

import { downloadNative, exportRequest, preparePdfExport, type ExportTarget } from "./prepareExport";

export function useBatchDownload(onPreparingChange: (preparing: boolean) => void) {
  const toast = useToast();
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null);
  const [error, setError] = useState("");
  const active = useRef<{ controller: AbortController; release: () => void } | null>(null);
  useEffect(() => () => {
    active.current?.controller.abort();
    active.current?.release();
  }, []);

  const cancel = () => {
    active.current?.controller.abort();
    active.current?.release();
    setProgress(null);
  };

  const start = async (targets: readonly ExportTarget[]) => {
    if (active.current || targets.length < 2) return;
    setError("");
    if (targets.length > 20) {
      setError("单次最多打包 20 篇 PDF");
      return;
    }
    const frozen = targets.map((target) => ({ ...target }));
    const controller = new AbortController();
    const signal = controller.signal;
    const release = () => {
      if (active.current?.controller !== controller) return;
      active.current = null;
      onPreparingChange(false);
    };
    active.current = { controller, release };
    onPreparingChange(true);
    const items: ExportBundleItem[] = [];
    const prepare = async (indexes: number[]) => {
      for (const [position, index] of indexes.entries()) {
        signal.throwIfAborted();
        const target = frozen[index]!;
        setProgress({ current: position + 1, total: indexes.length });
        const result = await preparePdfExport(target, signal);
        items[index] = { job_id: target.jobId, mode: target.mode, side: target.side, snapshot: result.snapshot };
      }
    };
    const issue = () => exportRequest((requestSignal) => api.createExportBundle(items, requestSignal), signal);
    try {
      await prepare(frozen.map((_, index) => index));
      let bundle;
      try {
        bundle = await issue();
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 409 || !cause.staleIndexes?.length) throw cause;
        const indexes = [...new Set(cause.staleIndexes)];
        if (indexes.some((index) => index < 0 || index >= frozen.length)) throw cause;
        await prepare(indexes);
        bundle = await issue();
      }
      signal.throwIfAborted();
      downloadNative(bundle.download_url, bundle.filename);
      toast.success(`已开始打包下载 ${bundle.item_count} 篇 PDF`);
    } catch (cause) {
      if (!signal.aborted) {
        const message = cause instanceof Error ? cause.message : "打包下载失败，请重试";
        setError(message);
        toast.error(message);
      }
    } finally {
      if (active.current?.controller === controller) {
        setProgress(null);
        release();
      }
    }
  };

  return { start, cancel, progress, error };
}
