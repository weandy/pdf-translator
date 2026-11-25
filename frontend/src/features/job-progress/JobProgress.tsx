import { useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { deleteJob, STAGE_LABELS, STAGE_ORDER } from "@/entities/job/model";
import { api, type Job } from "@/shared/api/client";
import { askNotify, notifyState, type NotifyState } from "@/shared/attention/attention";
import { useConfirm } from "@/shared/feedback/ConfirmDialog";
import { useToast } from "@/shared/feedback/Toast";
import { ErrorCard } from "@/shared/feedback/ErrorCard";
import { formatDuration, formatRemaining, useNow } from "@/shared/time/elapsed";

import { useMineruQueueElapsed } from "./pending";

interface Props {
  job: Job;
  onRetried: () => void;
}

/** 只有解析（逐页）和翻译（逐批）有真实的完成比例，其余阶段显示“仍在处理”的动画条。 */
const HAS_FRACTION = new Set(["parse", "translate"]);

export function JobProgress({ job, onRetried }: Props) {
  const navigate = useNavigate();
  const confirm = useConfirm();
  const toast = useToast();
  const latest = useRef(job);
  useLayoutEffect(() => { latest.current = job; }, [job]);
  const inFlight = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"retry" | "cancel" | "delete" | null>(null);
  const [notifying, setNotifying] = useState<NotifyState>(notifyState);
  const live = job.status === "running" || job.status === "queued";
  const now = useNow(live);
  const cloudQueueElapsed = useMineruQueueElapsed(job);
  const current = STAGE_ORDER.indexOf(job.stage);
  const since = (iso: string | null | undefined) => (iso ? formatDuration(now - Date.parse(iso)) : null);

  const run = async (action: "retry" | "cancel" | "delete") => {
    if (inFlight.current) return;
    if (action === "cancel" && !(await confirm({
      title: "取消翻译",
      message: "取消这个任务？已完成的步骤会保留，之后可以从中断处重新开始。",
      confirmLabel: "确认取消",
    }))) return;
    if (action === "delete" && !(await confirm({
      title: "删除任务",
      message: `删除“${job.filename}”的原文与译文？删除后无法恢复。`,
      confirmLabel: "确认删除",
      tone: "danger",
    }))) return;
    const currentJob = latest.current;
    const active = currentJob.status === "running" || currentJob.status === "queued";
    if (inFlight.current || job.id !== currentJob.id || active !== (action === "cancel")) return;
    inFlight.current = true;
    setBusy(action);
    setError(null);
    try {
      if (action === "retry") {
        await api.retryJob(job.id, { channelMode: currentJob.channel_mode });
        toast.info("已提交重试请求");
        onRetried();
      } else if (action === "cancel") {
        await api.cancelJob(job.id);
        toast.info("已提交取消请求，正在停止任务…");
      } else {
        await deleteJob(job.id);
        toast.success("已删除任务");
        void navigate("/");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(message);
      toast.error(message);
    } finally {
      inFlight.current = false;
      setBusy(null);
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
      {job.expires_at && (
        <p className="hint">原文与译文在服务器上{formatRemaining(job.expires_at)}，到期自动删除，完成后请及时下载。</p>
      )}

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
                  {stage === "parse" && cloudQueueElapsed > 0 && (
                    <p className="wait-note" role="status">
                      当前 MinerU 云端队列较忙，已等待 {formatDuration(cloudQueueElapsed)}。可以先关闭此页面，稍后在首页「我的任务」查看进度。
                    </p>
                  )}
                </div>
              )}
              {(state === "failed" || state === "cancelled") && (
                <div className="stage-detail failure">
                  <ErrorCard
                    title={state === "failed" ? `${STAGE_LABELS[job.stage] ?? "处理"}失败` : "任务已取消"}
                    message={
                      state === "failed"
                        ? job.error || "这一步没有完成，可以从中断处继续。"
                        : "已完成的步骤会保留，可以从中断处重新开始。"
                    }
                    actionLabel={state === "failed" ? "从中断处重试" : "重新开始"}
                    busy={busy !== null} onAction={() => void run("retry")}
                    secondaryLabel="删除"
                    onSecondary={() => void run("delete")}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ol>
      {live && (
        <div className="progress-actions">
          {notifying === "granted" ? (
            <span className="hint">完成时会发桌面通知（页面在后台时）</span>
          ) : notifying === "denied" ? (
            <span className="hint">浏览器拒绝了通知，可在地址栏的网站设置里打开</span>
          ) : notifying === "default" ? (
            <button type="button" className="quiet" onClick={() => void askNotify().then(setNotifying)}>
              完成时通知我
            </button>
          ) : null}
          <button type="button" className="quiet danger" disabled={busy !== null} onClick={() => void run("cancel")}>
            {busy === "cancel" ? "正在取消…" : "取消翻译"}
          </button>
        </div>
      )}
      {error && <ErrorCard title="操作没有完成" message={error} actionLabel="再试一次" busy={busy !== null} onAction={() => void run("retry")} />}
    </section>
  );
}
