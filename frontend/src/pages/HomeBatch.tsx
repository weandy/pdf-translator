import { useCallback, useId, useState } from "react";
import { Link } from "react-router";

import type { BatchEntry } from "@/entities/batch/model";
import { STAGE_LABELS } from "@/entities/job/model";
import { DEFAULT_DOWNLOAD_MODE, DownloadFormatSelect } from "@/features/export/DownloadChoice";
import { ExportDownloadLink } from "@/features/export/ExportDownloadLink";
import type { Job } from "@/shared/api/client";

const SUMMARY_LABELS = {
  done: "可下载",
  active: "处理中",
  failed: "失败",
  cancelled: "已取消",
  unavailable: "不可用",
  pending: "查询中",
  unreadable: "暂时无法读取",
};

type BatchStatus = keyof typeof SUMMARY_LABELS;

function memberStatus(job: Job | undefined, unavailable: boolean, unreadable: boolean): BatchStatus {
  if (job) return job.status === "queued" || job.status === "running" ? "active" : job.status;
  if (unavailable) return "unavailable";
  return unreadable ? "unreadable" : "pending";
}

interface Props {
  batch: BatchEntry;
  jobs: ReadonlyMap<string, Job>;
  filenames: ReadonlyMap<string, string>;
  unavailable: ReadonlySet<string>;
  unreadable: ReadonlySet<string>;
  onCancelJob: (id: string, filename: string) => Promise<void>;
  onDeleteJob: (id: string, filename: string) => Promise<void>;
  onRemoveBatch: (batch: BatchEntry) => void;
}

export function HomeBatch({
  batch,
  jobs,
  filenames,
  unavailable,
  unreadable,
  onCancelJob,
  onDeleteJob,
  onRemoveBatch,
}: Props) {
  const titleId = useId();
  const [mode, setMode] = useState(DEFAULT_DOWNLOAD_MODE);
  const [preparingCount, setPreparingCount] = useState(0);
  const onPreparingChange = useCallback((preparing: boolean) => {
    setPreparingCount((count) => count + (preparing ? 1 : -1));
  }, []);
  const counts: Record<BatchStatus, number> = {
    done: 0,
    active: 0,
    failed: 0,
    cancelled: 0,
    unavailable: 0,
    pending: 0,
    unreadable: 0,
  };
  const entries = batch.jobIds.map((id, index) => {
    const job = jobs.get(id);
    const status = memberStatus(job, unavailable.has(id), unreadable.has(id));
    counts[status] += 1;
    return { id, job, status, filename: job?.filename ?? filenames.get(id) ?? `论文 ${index + 1}` };
  });
  const finished = counts.done + counts.failed + counts.cancelled + counts.unavailable;
  // 向下取整，仍有论文未确认结果时，进度不能提前显示 100%。
  const percent = Math.floor((finished / Math.max(entries.length, 1)) * 100);
  const summary = (Object.keys(SUMMARY_LABELS) as BatchStatus[])
    .filter((status) => counts[status] > 0)
    .map((status) => `${counts[status]} 篇${SUMMARY_LABELS[status]}`);
  if (percent === 100) summary.unshift("已结束");

  return (
    <li className="batch-card">
      <div className="batch-card-head">
        <div className="batch-card-info">
          <h3 id={titleId} title={batch.name}>
            {batch.name}
          </h3>
          <p className="hint">{summary.join(" · ")}</p>
        </div>
        <div className="batch-format">
          <span aria-hidden="true">下载格式</span>
          <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparingCount > 0} label="批次下载格式" />
        </div>
      </div>
      <div
        className="batch-progress"
        role="progressbar"
        aria-labelledby={titleId}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <ul className="batch-items">
        {entries.map(({ id, job, status, filename }) => (
          <li key={id}>
            {job ? (
              <Link to={`/jobs/${id}`} className="name" title={filename}>
                {filename}
              </Link>
            ) : (
              <span className="name muted" title={filename}>
                {filename}
              </span>
            )}
            <span className={`status ${job?.status ?? status}`} title={job?.message || undefined}>
              {job?.status === "running"
                ? (STAGE_LABELS[job.stage] ?? SUMMARY_LABELS.active)
                : job?.status === "queued"
                  ? "排队中"
                  : status === "unavailable"
                    ? "已过期或已删除"
                    : SUMMARY_LABELS[status]}
            </span>
            {job && (
              <div className="batch-row-actions">
                {status === "done" && (
                  <div className="batch-row-download">
                    <ExportDownloadLink
                      key={mode}
                      className="button"
                      jobId={id}
                      mode={mode}
                      onPreparingChange={onPreparingChange}
                    >
                      下载 PDF
                    </ExportDownloadLink>
                  </div>
                )}
                {status === "active" ? (
                  <button type="button" className="quiet" onClick={() => void onCancelJob(id, filename)}>
                    取消
                  </button>
                ) : (
                  <button type="button" className="quiet danger" onClick={() => void onDeleteJob(id, filename)}>
                    删除
                  </button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
      <div className="batch-card-footer">
        <button type="button" className="quiet" onClick={() => onRemoveBatch(batch)}>
          移除批次
        </button>
      </div>
    </li>
  );
}
