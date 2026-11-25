import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type Expansion, type Job } from "@/shared/api/client";
import { useToast } from "@/shared/feedback/Toast";

export function useExpansion(job: Job | null, id: string, refresh: () => void) {
  const toast = useToast();
  const [submitted, setSubmitted] = useState<Expansion | null>(null);
  const previous = useRef<Expansion | null>(null);
  const notified = useRef("");
  const version = useRef({ id, value: 0 });
  const server = job?.active_expansion;
  const local = submitted?.job_id === id ? submitted : null;
  const tracked = server ?? local;
  const fallback = useQuery({
    queryKey: ["expansion", id, tracked?.id],
    queryFn: ({ signal }) => api.expansionStatus(id, tracked!.id, signal),
    enabled: Boolean(tracked && !server),
    refetchInterval: (query) => {
      const op = query.state.data;
      return !op || op.status === "queued" || op.status === "running" ? 3000 : false;
    },
  });

  useEffect(() => {
    if (server) previous.current = server;
    else if (previous.current?.job_id === id) {
      const finished = previous.current;
      previous.current = null;
      if ((job?.content_version ?? 1) < finished.target_version) {
        void api.expansionStatus(id, finished.id).then((op) => {
          if (notified.current === op.id) return;
          if (op.status === "failed") toast.error(op.error || "补翻失败");
          if (op.status === "cancelled") toast.info("已取消补翻");
          notified.current = op.id;
        }).catch((error: unknown) => {
          toast.error(error instanceof Error ? error.message : "无法确认补翻结果");
        });
      }
    }
  }, [id, job, server, toast]);

  useEffect(() => {
    const op = fallback.data;
    if (!op || op.status === "queued" || op.status === "running" || notified.current === op.id) return;
    notified.current = op.id;
    if (op.status === "failed") toast.error(op.error || "补翻失败");
    if (op.status === "cancelled") toast.info("已取消补翻");
    refresh();
  }, [fallback.data, refresh, toast]);

  const contentVersion = job?.content_version ?? 1;
  useEffect(() => {
    if (!job) return;
    if (version.current.id === id && version.current.value > 0 && contentVersion > version.current.value) {
      const count = job.published_pages?.length ?? job.options.pages?.length ?? job.page_count;
      toast.success(`补翻完成，已更新为 ${count}/${job.page_count} 页译文`);
    }
    version.current = { id, value: contentVersion };
  }, [contentVersion, id, job, toast]);

  const pending = fallback.data ?? local;
  const active = server ?? (pending?.status === "queued" || pending?.status === "running" ? pending : null);
  return {
    active,
    started: (op: Expansion) => {
      setSubmitted(op);
      refresh();
    },
  };
}
