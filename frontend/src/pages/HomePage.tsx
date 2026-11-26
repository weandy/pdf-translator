import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { pruneBatches, readBatches, rememberBatch, removeBatch, type BatchEntry } from "@/entities/batch/model";
import { removeHistory, useHistory } from "@/entities/history/model";
import { deleteJob, MY_JOBS_QUERY, myJobIds, restoreMyJobs, useMyJobs } from "@/entities/job/model";
import { TermsModal } from "@/features/legal/TermsModal";
import { UploadPanel } from "@/features/upload/UploadPanel";
import { HomeTaskCard } from "@/pages/HomeBatch";
import { api, type Job } from "@/shared/api/client";
import { APP_TITLE, notify } from "@/shared/attention/attention";
import { useConfirm } from "@/shared/feedback/ConfirmDialog";
import { useToast } from "@/shared/feedback/Toast";

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
  const confirm = useConfirm();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const [termsOpen, setTermsOpen] = useState(false);
  const history = useHistory();
  const [batches, setBatches] = useState<BatchEntry[]>(readBatches);
  const batchIds = new Set(batches.flatMap((batch) => batch.jobIds));
  const { data: jobsResult, isPending } = useMyJobs([...batchIds]);
  const jobsById = new Map(jobsResult.jobs.map((job) => [job.id, job]));
  const filenames = new Map(history.map((entry) => [entry.id, entry.filename]));
  const unavailable = new Set(jobsResult.unavailableIds);
  const unavailableRef = useRef(jobsResult.unavailableIds);
  useLayoutEffect(() => { unavailableRef.current = jobsResult.unavailableIds; }, [jobsResult.unavailableIds]);
  const unavailableKey = jobsResult.unavailableIds.join(",");
  useEffect(() => {
    if (!unavailableKey) return;
    const { promotedJobIds } = pruneBatches(unavailableKey.split(","));
    if (promotedJobIds.length > 0) restoreMyJobs(promotedJobIds);
  }, [unavailableKey]);
  const unreadable = new Set(jobsResult.failedIds);
  const trackedIds = new Set(myJobIds());
  const historyById = new Map(history.map((entry) => [entry.id, entry]));
  const tasks: TaskEntry[] = [];
  for (const batch of batches) {
    const liveIds = batch.jobIds.filter((id) => !unavailable.has(id));
    if (liveIds.length >= 2) {
      tasks.push({ key: `group-${batch.id}`, jobIds: liveIds, createdAt: batch.createdAt, batch });
    } else if (liveIds.length === 1 && liveIds[0]) {
      const id = liveIds[0];
      const createdAt = jobsById.get(id)?.created_at ?? historyById.get(id)?.createdAt ?? batch.createdAt;
      tasks.push({ key: id, jobIds: [id], createdAt });
    }
  }
  for (const id of new Set([...trackedIds, ...jobsById.keys()])) {
    if (batchIds.has(id) || unavailable.has(id)) continue;
    const createdAt = jobsById.get(id)?.created_at ?? historyById.get(id)?.createdAt;
    tasks.push({ key: id, jobIds: [id], createdAt });
  }
  tasks.sort((a, b) => uploadTimestamp(b) - uploadTimestamp(a));
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
    if (!(await confirm({ title: "取消翻译", message: `取消“${filename}”？已完成的步骤会保留，之后可以重新开始。`, confirmLabel: "确认取消" }))) return;
    const job = queryClient.getQueryData<Job>([...MY_JOBS_QUERY, id]);
    if (job?.status !== "queued" && job?.status !== "running") return;
    await updateJob(id, await api.cancelJob(id));
    toast.info("已提交取消请求，正在停止任务…");
  };

  const remove = async (id: string, filename: string) => {
    if (!(await confirm({ title: "删除任务", message: `删除“${filename}”的原文与译文？删除后无法恢复。`, confirmLabel: "确认删除", tone: "danger" }))) return;
    const job = queryClient.getQueryData<Job>([...MY_JOBS_QUERY, id]);
    if (!job || job.status === "running" || job.status === "queued") return;
    await deleteJob(id);
    removeHistory(id);
    const pruned = pruneBatches([id]);
    if (pruned.promotedJobIds.length > 0) restoreMyJobs(pruned.promotedJobIds);
    if (pruned.changed) setBatches(pruned.batches);
    await updateJob(id, null);
    toast.success("已删除任务");
  };

  const retry = async (id: string) => {
    const job = jobsById.get(id);
    const next = job?.channel_mode === "free" ? await api.retryJob(id, { mode: "free" }) : await api.retryJob(id);
    await updateJob(id, next);
    toast.info("已提交重试请求");
  };

  const ungroup = async (id: string) => {
    if (!(await confirm({ title: "取消分组", message: "将这次上传的论文分开显示？每篇仍可单独打开和下载。", confirmLabel: "分开显示" }))) return;
    const batch = readBatches().find((entry) => entry.id === id);
    if (!batch) return;
    restoreMyJobs(batch.jobIds.filter((jobId) => !unavailableRef.current.includes(jobId)));
    removeBatch(id);
    setBatches(readBatches());
    toast.success("已分开显示本批任务");
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
          <p className="lead">上传 PDF，译文排回原位，与原文左右对照，支持逐段编辑并导出 PDF。</p>
          <ul className="facts">
            <li>
              <strong>公式与图表保留</strong>
              <span>行内公式按 LaTeX 重新渲染，图表与表格保持原位。</span>
            </li>
            <li>
              <strong>双语对照可编辑</strong>
              <span>逐段审校与术语微调，支持导出中文版或双语对照 PDF。</span>
            </li>
            <li>
              <strong>{config?.free_channel?.enabled ? "免 Key 直接试用" : "Key 只在本机"}</strong>
              <span>
                {config?.free_channel?.enabled
                  ? "支持免填 Key 直接体验公开论文；自备 API Key 享独立队列且仅存本机。"
                  : "自备 API Key 仅存在当前浏览器中，翻译前提供透明费用预估。"}
              </span>
            </li>
          </ul>
        </div>
        <UploadPanel
          onCreated={(job) => void navigate(`/jobs/${job.id}`)}
          onBatchCreated={(created) => {
            rememberBatch(created.map((job) => job.id));
            for (const job of created) queryClient.setQueryData([...MY_JOBS_QUERY, job.id], job);
            setBatches(readBatches());
            toast.success(`已提交 ${created.length} 篇论文。`);
          }}
          onNeedSettings={openSettings}
          onOpenTerms={() => setTermsOpen(true)}
        />
      </div>
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
                unreadable={unreadable}
                onCancelJob={cancel}
                onDeleteJob={remove}
                onRetryJob={retry}
                onUngroup={batch ? () => void ungroup(batch.id) : undefined}
              />
            ))}
          </ul>
        </section>
      )}
      <footer className="home-footer" aria-label="条款与说明">
        <button type="button" className="home-footer-btn" onClick={() => setTermsOpen(true)}>
          服务条款与隐私说明（ToS）
        </button>
        <span className="home-footer-sep" aria-hidden="true">
          ·
        </span>
        <Link to="/tos">独立条款页</Link>
        <span className="home-footer-sep" aria-hidden="true">
          ·
        </span>
        <Link to="/guide">使用指南</Link>
      </footer>
      {termsOpen && <TermsModal config={config} onClose={() => setTermsOpen(false)} />}
    </main>
  );
}
