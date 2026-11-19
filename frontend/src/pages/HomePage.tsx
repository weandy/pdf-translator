import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { readBatches, rememberBatch, removeBatch, type BatchEntry } from "@/entities/batch/model";
import { clearHistory, removeHistory, useHistory } from "@/entities/history/model";
import { deleteJob, MY_JOBS_QUERY, myJobIds, restoreMyJobs, STAGE_LABELS, useMyJobs } from "@/entities/job/model";
import { DownloadChoice } from "@/features/export/DownloadChoice";
import { UploadPanel } from "@/features/upload/UploadPanel";
import { HomeBatch } from "@/pages/HomeBatch";
import { api } from "@/shared/api/client";
import { APP_TITLE, notify } from "@/shared/attention/attention";
import { expiresSoon, formatRemaining } from "@/shared/time/elapsed";

const STATUS: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function HomePage({ openSettings }: { openSettings: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const history = useHistory();
  const [batches, setBatches] = useState<BatchEntry[]>(readBatches);
  const batchIds = new Set(batches.flatMap((batch) => batch.jobIds));
  const { data: jobsResult, isPending } = useMyJobs([...batchIds]);
  const jobsById = new Map(jobsResult.jobs.map((job) => [job.id, job]));
  const filenames = new Map(history.map((entry) => [entry.id, entry.filename]));
  const unavailable = new Set(jobsResult.unavailableIds);
  const unreadable = new Set(jobsResult.failedIds);
  const trackedIds = new Set(myJobIds());
  const jobs = jobsResult.jobs.filter((job) => !batchIds.has(job.id));
  const past = history.filter(
    (entry) =>
      !batchIds.has(entry.id) && !jobsById.has(entry.id) && !unreadable.has(entry.id) && !trackedIds.has(entry.id),
  );
  const active = jobsResult.jobs.filter((job) => job.status === "queued" || job.status === "running").length;
  const hasUnknown = isPending || unreadable.size > 0;
  const wasActive = useRef(0);
  useEffect(() => {
    // 查询未确认时保留通知计数，防止网络故障触发完成通知。
    if (!hasUnknown) {
      if (wasActive.current > 0 && active === 0) notify("翻译全部结束", "在首页查看翻译结果");
      wasActive.current = active;
    } else if (active > 0) {
      wasActive.current = active;
    }
    document.title = active > 0 ? `(进行中 ${active}) ${APP_TITLE}` : APP_TITLE;
  }, [active, hasUnknown]);

  const cancel = async (id: string, filename: string) => {
    if (!window.confirm(`取消“${filename}”？已完成的步骤会保留，之后可以重新开始。`)) return;
    setError(null);
    try {
      await api.cancelJob(id);
      await queryClient.invalidateQueries({ queryKey: MY_JOBS_QUERY });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (id: string, filename: string) => {
    if (!window.confirm(`删除“${filename}”的原文与译文？删除后无法恢复。`)) return;
    setError(null);
    try {
      await deleteJob(id);
      await queryClient.invalidateQueries({ queryKey: MY_JOBS_QUERY });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const ungroup = (batch: BatchEntry) => {
    if (!window.confirm("只移除这个批次分组，不删除其中的任务？")) return;
    restoreMyJobs(batch.jobIds.filter((id) => !unavailable.has(id)));
    removeBatch(batch.id);
    setBatches(readBatches());
    setNotice(null);
  };

  return (
    <main className="home">
      <div className="home-hero">
        <div className="home-intro">
          <h1>
            英文论文，
            <br />
            按原版式读中文
          </h1>
          <p className="lead">
            上传 PDF，译文排回原来的位置，与原文左右对照。公式、图表和表格保持原样，读完可以下载 PDF。
          </p>
          <ul className="facts">
            <li>
              <strong>公式不走样</strong>
              <span>行内公式交给模型时附带 LaTeX，译文里重新渲染；不会被翻成乱码。</span>
            </li>
            <li>
              <strong>一篇几分钱</strong>
              <span>
                用 DeepSeek 翻一篇 15 页论文约 <span className="nowrap">¥0.03–0.07</span>，翻译前会先给出预估。
              </span>
            </li>
            <li>
              <strong>Key 只在本机</strong>
              <span>API Key 只存在这个浏览器里，本站后端不保存。</span>
            </li>
          </ul>
        </div>
        <UploadPanel
          onCreated={(job) => void navigate(`/jobs/${job.id}`)}
          onBatchCreated={(created) => {
            const batch = rememberBatch(
              created.map((job) => job.id),
              created.length > 0 ? `${created[0]?.filename ?? "论文"} 等 ${created.length} 篇` : undefined,
            );
            setBatches(readBatches());
            setNotice(`已加入 ${created.length} 篇，进度见下方“${batch ? "我的批次" : "我的任务"}”。`);
            void queryClient.invalidateQueries({ queryKey: MY_JOBS_QUERY });
          }}
          onNeedSettings={openSettings}
        />
      </div>
      {notice && (
        <p className="home-feedback check-ok" role="status">
          {notice}
        </p>
      )}
      {(error || jobsResult.warning) && (
        <p className="home-feedback error" role="status">
          {error ?? jobsResult.warning}
        </p>
      )}
      {batches.length > 0 && (
        <section className="jobs batches" aria-labelledby="batches-title">
          <div className="jobs-head">
            <h2 id="batches-title">我的批次</h2>
          </div>
          <ul className="batch-list-home">
            {batches.map((batch) => (
              <HomeBatch
                key={batch.id}
                batch={batch}
                jobs={jobsById}
                filenames={filenames}
                unavailable={unavailable}
                unreadable={unreadable}
                onCancelJob={cancel}
                onDeleteJob={remove}
                onRemoveBatch={ungroup}
              />
            ))}
          </ul>
        </section>
      )}

      {jobs.length > 0 && (
        <section className="jobs" aria-labelledby="jobs-title">
          <div className="jobs-head">
            <h2 id="jobs-title">我的任务</h2>
            <span className="hint">只显示在这个浏览器里上传的论文</span>
          </div>
          <ul className="job-list">
            {jobs.map((j) => (
              <li key={j.id} className="job-item">
                <Link to={`/jobs/${j.id}`} className="name" title={j.filename}>
                  {j.filename}
                </Link>
                <div className="job-state" title={j.message || undefined}>
                  <span className={`status ${j.status}`}>
                    {j.status === "running" ? (STAGE_LABELS[j.stage] ?? STATUS.running) : STATUS[j.status]}
                  </span>
                  {(j.status === "running" || j.status === "queued") && j.message && (
                    <small className="job-message">{j.message}</small>
                  )}
                  {j.status !== "running" && j.status !== "queued" && j.expires_at && (
                    <small className={`job-message${expiresSoon(j.expires_at) ? " expiring" : ""}`}>
                      服务器保留{formatRemaining(j.expires_at)}
                    </small>
                  )}
                </div>
                <span className="facts-cell num">
                  {j.page_count} 页{j.cost_cny !== null && `，¥${j.cost_cny.toFixed(3)}`}
                </span>
                <span className="row-actions">
                  {j.status === "done" && <DownloadChoice jobId={j.id} />}
                  {(j.status === "running" || j.status === "queued") && (
                    <button type="button" className="quiet" onClick={() => void cancel(j.id, j.filename)}>
                      取消
                    </button>
                  )}
                  {j.status !== "running" && j.status !== "queued" && (
                    <button type="button" className="quiet danger" onClick={() => void remove(j.id, j.filename)}>
                      删除
                    </button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {past.length > 0 && (
        <section className="jobs history" aria-labelledby="history-title">
          <div className="jobs-head">
            <h2 id="history-title">历史记录</h2>
            <span className="hint">只存在这个浏览器里</span>
            <span className="spacer" />
            <button
              type="button"
              className="quiet"
              onClick={() => window.confirm("清除这个浏览器里的全部翻译历史？") && clearHistory()}
            >
              全部清除
            </button>
          </div>
          <ul className="job-list">
            {past.map((h) => (
              <li key={h.id} className="job-item">
                <span className="name" title={h.filename}>
                  {h.filename}
                </span>
                <span className="status">{unavailable.has(h.id) ? "已过期或已删除" : "历史记录"}</span>
                <span className="facts-cell num">
                  {h.pages} 页，{new Date(h.createdAt).toLocaleDateString("zh-CN")}
                </span>
                <span className="row-actions">
                  <button type="button" className="quiet" onClick={() => removeHistory(h.id)}>
                    移除
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
