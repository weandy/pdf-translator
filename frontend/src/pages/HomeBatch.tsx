import { useCallback, useState } from "react";

import type { BatchEntry } from "@/entities/batch/model";
import { DEFAULT_DOWNLOAD_MODE, DownloadFormatSelect } from "@/features/export/DownloadChoice";
import type { Job } from "@/shared/api/client";

import { HomeJobRow, type HomeJobActions } from "./HomeJobRow";

const uploadTime = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

interface Props extends HomeJobActions {
  batch: BatchEntry;
  jobs: ReadonlyMap<string, Job>;
  filenames: ReadonlyMap<string, string>;
  unavailable: ReadonlySet<string>;
  unreadable: ReadonlySet<string>;
  onRemoveBatch: (batch: BatchEntry) => void;
}

export function HomeBatch({ batch, jobs, filenames, unavailable, unreadable, onRemoveBatch, ...actions }: Props) {
  const [mode, setMode] = useState(DEFAULT_DOWNLOAD_MODE);
  const [preparingCount, setPreparingCount] = useState(0);
  const onPreparingChange = useCallback((preparing: boolean) => {
    setPreparingCount((count) => count + (preparing ? 1 : -1));
  }, []);
  const ids = [...new Set(batch.jobIds)];
  const downloadable = ids.filter((id) => jobs.get(id)?.status === "done").length;
  const createdAt = Date.parse(batch.createdAt);
  const groupDownload = { mode, onPreparingChange };

  return (
    <li className="batch-card">
      <div className="batch-card-head">
        <div className="batch-card-info">
          <h3>
            {Number.isFinite(createdAt) ? (
              <>
                <time dateTime={batch.createdAt}>{uploadTime.format(createdAt)}</time> 上传的论文
              </>
            ) : (
              "一起上传的论文"
            )}
          </h3>
          <p className="hint">
            可下载 {downloadable} / {ids.length} 篇
          </p>
        </div>
        {downloadable > 0 && (
          <div className="batch-format">
            <span aria-hidden="true">下载格式</span>
            <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparingCount > 0} />
          </div>
        )}
      </div>
      <ul className="batch-items">
        {ids.map((id, index) => {
          const job = jobs.get(id);
          return (
            <HomeJobRow
              key={id}
              id={id}
              filename={job?.filename ?? filenames.get(id) ?? `论文 ${index + 1}`}
              job={job}
              lookupState={unavailable.has(id) ? "unavailable" : unreadable.has(id) ? "unreadable" : "pending"}
              groupDownload={groupDownload}
              {...actions}
            />
          );
        })}
      </ul>
      <div className="batch-card-footer">
        <button type="button" className="quiet" onClick={() => onRemoveBatch(batch)}>
          取消分组
        </button>
      </div>
    </li>
  );
}
