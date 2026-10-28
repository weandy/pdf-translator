import { useState } from "react";
import { Link } from "react-router";

import { STAGE_LABELS, STAGE_ORDER } from "@/entities/job/model";
import { api, type Job } from "@/shared/api/client";
import { formatDuration, useNow } from "@/shared/time/elapsed";

interface Props {
  job: Job;
  onRetried: () => void;
}

/** 只有解析（逐页）和翻译（逐批）有真实的完成比例，其余阶段显示“仍在处理”的动画条。 */
const HAS_FRACTION = new Set(["parse", "translate"]);

export function JobProgress({ job, onRetried }: Props) {
  const [error, setError] = useState<string | null>(null);
  const live = job.status === "running" || job.status === "queued";
  const now = useNow(live);
  const current = STAGE_ORDER.indexOf(job.stage);
  const since = (iso: string | null | undefined) => (iso ? formatDuration(now - Date.parse(iso)) : null);

  const retry = async () => {
    setError(null);
    try {
      await api.retryJob(job.id);
      onRetried();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="progress" aria-live="polite">
      <Link to="/" className="back">
        返回首页
      </Link>
      <h1 className="file-title">{job.filename}</h1>
      <p className="elapsed hint num">
        {job.status === "queued" && "排队中，前面的任务完成后开始。"}
        {job.status === "running" && `已用时 ${since(job.created_at) ?? ""}，共 ${job.page_count} 页`}
        {job.status === "failed" && "任务没有完成。已完成的步骤会保留，重试时从中断处继续，不会重复收费。"}
      </p>

      <ol className="timeline">
        {STAGE_ORDER.map((stage, i) => {
          const state =
            job.status === "done" || i < current
              ? "done"
              : i === current
                ? job.status === "failed"
                  ? "failed"
                  : "active"
                : "";
          const fraction = HAS_FRACTION.has(stage) && job.progress > 0 ? job.progress : null;
          return (
            <li key={stage} className={state} aria-current={state === "active" ? "step" : undefined}>
              <span className="stage-name">{STAGE_LABELS[stage]}</span>
              {state === "active" && (
                <div className="stage-detail">
                  <div className="row">
                    <p>{job.message || "处理中"}</p>
                    {job.stage_started_at && <span className="hint num">已等待 {since(job.stage_started_at)}</span>}
                  </div>
                  <div
                    className={`bar${fraction === null ? " indeterminate" : ""}`}
                    role="progressbar"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={fraction === null ? undefined : Math.round(fraction * 100)}
                  >
                    <span style={fraction === null ? undefined : { width: `${fraction * 100}%` }} />
                  </div>
                  {stage === "parse" && (
                    <p className="wait-note">
                      MinerU 免费额度在高峰时可能要排队 10 分钟以上。可以先关闭这个页面，之后在首页“我的任务”里打开。
                    </p>
                  )}
                </div>
              )}
              {state === "failed" && (
                <div className="stage-detail failure">
                  <p className="error">{job.error || "这一步失败了"}</p>
                  <button type="button" className="primary" onClick={() => void retry()}>
                    从中断处重试
                  </button>
                  {error && <p className="error">{error}</p>}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
