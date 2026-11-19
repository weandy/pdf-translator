import { DownloadFormatSelect, useDownloadChoice } from "@/features/export/DownloadChoice";
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
  unavailable: ReadonlySet<string>;
  unreadable: ReadonlySet<string>;
  onUngroup?: () => void;
}

export function HomeTaskCard({
  jobIds,
  createdAt,
  jobs,
  filenames,
  unavailable,
  unreadable,
  onUngroup,
  ...actions
}: Props) {
  const { mode, setMode, preparing, onPreparingChange } = useDownloadChoice();
  const ids = [...new Set(jobIds)];
  const downloadable = ids.filter((id) => jobs.get(id)?.status === "done").length;
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
        {downloadable > 0 && (
          <div className="batch-format">
            <span aria-hidden="true">下载格式</span>
            <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparing} />
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
