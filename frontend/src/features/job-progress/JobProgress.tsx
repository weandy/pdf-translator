import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { deleteJob, STAGE_LABELS, STAGE_ORDER } from "@/entities/job/model";
import { api, type Job } from "@/shared/api/client";
import { formatDuration, useNow } from "@/shared/time/elapsed";

interface Props {
  job: Job;
  onRetried: () => void;
}

/** 只有解析（逐页）和翻译（逐批）有真实的完成比例，其余阶段显示“仍在处理”的动画条。 */
const HAS_FRACTION = new Set(["parse", "translate"]);

export function JobProgress({ job, onRetried }: Props) {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const live = job.status === "running" || job.status === "queued";
  const stopped = job.status === "failed" || job.status === "cancelled";
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

  const cancel = async () => {
    if (!window.confirm("取消这个任务？已完成的步骤会保留，之后可以从中断处重新开始。")) return;
    setError(null);
    setCancelling(true);
    try {
      await api.cancelJob(job.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancelling(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`删除“${job.filename}”的原文与译文？删除后无法恢复。`)) return;
    try {
      await deleteJob(job.id);
      void navigate("/");
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
        {job.status === "cancelled" && "已取消。已完成的步骤会保留，重新开始时从中断处继续，已翻过的段落不会重复收费。"}
      </p>

      <ol className="timeline">
        {STAGE_ORDER.map((stage, i) => {
          const state =
            job.status === "done" || i < current
              ? "done"
              : i === current
                ? job.status === "failed"
                  ? "failed"
                  : job.status === "cancelled"
                    ? "cancelled"
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
                  {stage === "parse" && job.options.parser === "mineru-cloud" && (
                    <p className="wait-note">
                      MinerU 免费额度在高峰时可能要排队 10 分钟以上。可以先关闭这个页面，之后在首页“我的任务”里打开。
                    </p>
                  )}
                </div>
              )}
              {(state === "failed" || state === "cancelled") && (
                <div className="stage-detail failure">
                  {state === "failed" ? <p className="error">{job.error || "这一步失败了"}</p> : <p>在这一步取消了</p>}
                  <div className="row-actions">
                    <button type="button" className="primary" onClick={() => void retry()}>
                      {state === "failed" ? "从中断处重试" : "重新开始"}
                    </button>
                    <button type="button" className="quiet danger" onClick={() => void remove()}>
                      删除
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ol>
      {live && (
        <div className="progress-actions">
          <button type="button" className="quiet danger" disabled={cancelling} onClick={() => void cancel()}>
            {cancelling ? "正在取消…" : "取消翻译"}
          </button>
        </div>
      )}
      {stopped && error && <p className="error">{error}</p>}
      {live && error && <p className="error">{error}</p>}
    </section>
  );
}
