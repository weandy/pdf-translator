import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { readBatches, rememberBatch, removeBatch, type BatchEntry } from "@/entities/batch/model";
import { clearHistory, removeHistory, useHistory } from "@/entities/history/model";
import { deleteJob, MY_JOBS_QUERY, myJobIds, restoreMyJobs, useMyJobs } from "@/entities/job/model";
import { UploadPanel } from "@/features/upload/UploadPanel";
import { HomeTaskCard } from "@/pages/HomeBatch";
import { api, type Job } from "@/shared/api/client";
import { APP_TITLE, notify } from "@/shared/attention/attention";

interface TaskEntry {
  key: string;
  jobIds: string[];
  createdAt?: string;
  batch?: BatchEntry;
}

function uploadTimestamp(entry: TaskEntry): number {
  return Date.parse(entry.createdAt ?? "") || 0;
}

export function HomePage({ openSettings }: { openSettings: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
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
  const historyById = new Map(history.map((entry) => [entry.id, entry]));
  const tasks: TaskEntry[] = batches.map((batch) => ({
    key: `group-${batch.id}`,
    jobIds: batch.jobIds,
    createdAt: batch.createdAt,
    batch,
  }));
  for (const id of new Set([...trackedIds, ...jobsById.keys()])) {
    if (batchIds.has(id) || unavailable.has(id)) continue;
    const createdAt = jobsById.get(id)?.created_at ?? historyById.get(id)?.createdAt;
    tasks.push({ key: id, jobIds: [id], createdAt });
  }
  tasks.sort((a, b) => uploadTimestamp(b) - uploadTimestamp(a));
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

  const updateJob = async (id: string, job: Job | null) => {
    const queryKey = [...MY_JOBS_QUERY, id];
    // 在写入操作结果前结束旧的轮询请求，避免较晚返回的旧状态覆盖操作结果。
    await queryClient.cancelQueries({ queryKey, exact: true });
    queryClient.setQueryData(queryKey, job);
  };

  const cancel = async (id: string, filename: string) => {
    if (!window.confirm(`取消“${filename}”？已完成的步骤会保留，之后可以重新开始。`)) return;
    await updateJob(id, await api.cancelJob(id));
  };

  const remove = async (id: string, filename: string) => {
    if (!window.confirm(`删除“${filename}”的原文与译文？删除后无法恢复。`)) return;
    await deleteJob(id);
    await updateJob(id, null);
  };

  const retry = async (id: string) => {
    const job = jobsById.get(id);
    const next = job?.channel_mode === "free" ? await api.retryJob(id, { mode: "free" }) : await api.retryJob(id);
    await updateJob(id, next);
  };

  const ungroup = (batch: BatchEntry) => {
    if (!window.confirm("将这次上传的论文分开显示？每篇仍可单独打开和下载。")) return;
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
            {config?.free_channel?.enabled && (
              <li>
                <strong>免费体验入口</strong>
                <span>无需配置 Key 即可直接试用公开论文翻译；自备 Key 享有独立翻译队列。</span>
              </li>
            )}
          </ul>
        </div>
        <UploadPanel
          onCreated={(job) => void navigate(`/jobs/${job.id}`)}
          onBatchCreated={(created) => {
            rememberBatch(created.map((job) => job.id));
            for (const job of created) queryClient.setQueryData([...MY_JOBS_QUERY, job.id], job);
            setBatches(readBatches());
            setNotice(`已提交 ${created.length} 篇论文。`);
          }}
          onNeedSettings={openSettings}
        />
      </div>
      {notice && (
        <p className="home-feedback check-ok" role="status">
          {notice}
        </p>
      )}
      {tasks.length > 0 && (
        <section className="jobs current-jobs" aria-labelledby="jobs-title">
          <div className="jobs-head">
            <h2 id="jobs-title">我的任务</h2>
            <span className="hint">本机上传，最新在前</span>
          </div>
          <ul className="task-list">
            {tasks.map(({ key, jobIds, createdAt, batch }) => (
              <HomeTaskCard
                key={key}
                jobIds={jobIds}
                createdAt={createdAt}
                jobs={jobsById}
                filenames={filenames}
                unavailable={unavailable}
                unreadable={unreadable}
                onCancelJob={cancel}
                onDeleteJob={remove}
                onRetryJob={retry}
                onUngroup={batch ? () => ungroup(batch) : undefined}
              />
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
