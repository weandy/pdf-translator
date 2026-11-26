import { DownloadFormatSelect, useDownloadChoice } from "@/features/export/DownloadChoice";
import { useBatchDownload } from "@/features/export/useBatchDownload";
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
  jobIds: string[];
  createdAt: string | undefined;
  jobs: ReadonlyMap<string, Job>;
  filenames: ReadonlyMap<string, string>;
  unreadable: ReadonlySet<string>;
  onUngroup?: () => void;
}

export function HomeTaskCard({
  jobIds,
  createdAt,
  jobs,
  filenames,
  unreadable,
  onUngroup,
  ...actions
}: Props) {
  const ids = [...new Set(jobIds)];
  const cardKey = JSON.stringify([...ids].sort());
  const { mode, setMode, preparing, onPreparingChange } = useDownloadChoice(cardKey);
  const downloadableIds = ids.filter((id) => jobs.get(id)?.status === "done");
  const downloadable = downloadableIds.length;
  const batchDownload = useBatchDownload(onPreparingChange);
  const timestamp = Date.parse(createdAt ?? "");

  return (
    <li className="batch-card">
      <div className="batch-card-head">
        <div className="batch-card-info">
          <h3>
            {Number.isFinite(timestamp) ? (
              <>
                <time dateTime={createdAt}>{uploadTime.format(timestamp)}</time> 上传的论文
              </>
            ) : (
              "上传记录"
            )}
          </h3>
          <p className="hint">
            可下载 {downloadable} / {ids.length} 篇
          </p>
        </div>
        {(downloadable > 0 || batchDownload.progress) && (
          <div className="batch-format">
            <span aria-hidden="true">下载格式</span>
            <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparing} />
            {downloadable >= 2 && (
              <button type="button" disabled={preparing} onClick={() => void batchDownload.start(
                downloadableIds.map((jobId) => ({ jobId, mode, side: "right" })),
              )}>
                全部下载（{downloadable} 篇）
              </button>
            )}
            {batchDownload.progress && (
              <>
                <span role="status">准备中 {batchDownload.progress.current}/{batchDownload.progress.total}…</span>
                <button type="button" className="quiet" onClick={batchDownload.cancel}>取消</button>
              </>
            )}
            {batchDownload.error && <small role="alert" className="error">{batchDownload.error}</small> }
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
              lookupState={unreadable.has(id) ? "unreadable" : "pending"}
              downloadMode={mode}
              onPreparingChange={onPreparingChange}
              {...actions}
            />
          );
        })}
      </ul>
      {onUngroup && ids.length > 1 && (
        <div className="batch-card-footer">
          <button type="button" className="quiet" onClick={onUngroup}>
            取消分组
          </button>
        </div>
      )}
    </li>
  );
}
