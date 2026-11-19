import { useRef, useState } from "react";
import { Link } from "react-router";

import { STAGE_LABELS } from "@/entities/job/model";
import { ExportDownloadLink } from "@/features/export/ExportDownloadLink";
import type { ExportMode, Job } from "@/shared/api/client";
import { expiresSoon, formatRemaining } from "@/shared/time/elapsed";

const STATUS_LABELS: Record<Job["status"], string> = {
  queued: "排队中",
  running: "进行中",
  done: "可下载",
  failed: "失败",
  cancelled: "已取消",
};

const LOOKUP_LABELS = {
  pending: "查询中",
  unreadable: "暂时无法读取",
  unavailable: "已过期或已删除",
};

type JobAction = (id: string, filename: string) => Promise<void>;

export interface HomeJobActions {
  onCancelJob: JobAction;
  onDeleteJob: JobAction;
  onRetryJob: (id: string) => Promise<void>;
}

interface Props extends HomeJobActions {
  id: string;
  filename: string;
  job?: Job;
  lookupState?: keyof typeof LOOKUP_LABELS;
  downloadMode: ExportMode;
  onPreparingChange: (preparing: boolean) => void;
}

export function HomeJobRow({
  id,
  filename,
  job,
  lookupState = "pending",
  downloadMode,
  onPreparingChange,
  onCancelJob,
  onDeleteJob,
  onRetryJob,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const inFlight = useRef(false);
  const active = job?.status === "running" || job?.status === "queued";
  const canRetry = job?.status === "failed" || job?.status === "cancelled";
  let status = job ? STATUS_LABELS[job.status] : LOOKUP_LABELS[lookupState];
  if (job?.status === "running") status = STAGE_LABELS[job.stage] ?? status;
  const message = job?.error || job?.message;
  const remaining = job?.expires_at ? formatRemaining(job.expires_at) : null;

  const actOnJob = async (action: JobAction) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setActionError("");
    try {
      await action(id, filename);
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "操作未完成，请重试");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <li className="job-item">
      <div className="job-document">
        {job ? (
          <Link to={`/jobs/${id}`} className="name" title={filename}>
            {filename}
          </Link>
        ) : (
          <span className="name muted" title={filename}>
            {filename}
          </span>
        )}
        {job && (
          <small className="job-metadata num">
            {job.page_count} 页{job.cost_cny !== null && ` · ¥${job.cost_cny.toFixed(3)}`}
            {remaining && (
              <span className={expiresSoon(job.expires_at) ? "expiring" : undefined}>
                {remaining === "即将删除" ? ` · ${remaining}` : ` · 保留期${remaining}`}
              </span>
            )}
          </small>
        )}
      </div>
      <div className="job-state" title={message || undefined}>
        <span className={`status ${job?.status ?? lookupState}`}>{status}</span>
        {message && message !== status && <small className="job-message">{message}</small>}
      </div>
      {job && (
        <div className="row-actions">
          {job.status === "done" && (
            <div className="batch-row-download">
              <ExportDownloadLink
                key={downloadMode}
                className="button"
                jobId={id}
                mode={downloadMode}
                onPreparingChange={onPreparingChange}
              >
                下载 PDF
              </ExportDownloadLink>
            </div>
          )}
          {canRetry && (
            <button type="button" className="job-retry" disabled={busy} onClick={() => void actOnJob(onRetryJob)}>
              {job.status === "failed" ? "重试" : "重新开始"}
            </button>
          )}
          <button
            type="button"
            className="quiet job-secondary"
            disabled={busy}
            onClick={() => void actOnJob(active ? onCancelJob : onDeleteJob)}
          >
            {active ? "取消" : "删除"}
          </button>
          {actionError && (
            <small className="job-action-error" role="alert">
              {actionError}
            </small>
          )}
        </div>
      )}
    </li>
  );
}
