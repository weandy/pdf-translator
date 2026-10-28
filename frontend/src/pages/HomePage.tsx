import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { deleteJob, MY_JOBS_QUERY, STAGE_LABELS, useMyJobs } from "@/entities/job/model";
import { UploadPanel } from "@/features/upload/UploadPanel";
import { exportUrl } from "@/shared/api/client";

const STATUS: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  done: "已完成",
  failed: "失败",
};

export function HomePage({ openSettings }: { openSettings: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: jobs } = useMyJobs();
  const [error, setError] = useState<string | null>(null);

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
        <UploadPanel onCreated={(job) => void navigate(`/jobs/${job.id}`)} onNeedSettings={openSettings} />
      </div>

      {jobs && jobs.length > 0 && (
        <section className="jobs" aria-labelledby="jobs-title">
          <div className="jobs-head">
            <h2 id="jobs-title">我的任务</h2>
            <span className="hint">只显示在这个浏览器里上传的论文</span>
          </div>
          {error && <p className="error">{error}</p>}
          <ul className="job-list">
            {jobs.map((j) => (
              <li key={j.id} className="job-item">
                <Link to={`/jobs/${j.id}`} className="name" title={j.filename}>
                  {j.filename}
                </Link>
                <span className={`status ${j.status}`}>
                  {j.status === "running" ? (STAGE_LABELS[j.stage] ?? STATUS.running) : STATUS[j.status]}
                </span>
                <span className="facts-cell num">
                  {j.page_count} 页{j.cost_cny !== null && `，¥${j.cost_cny.toFixed(3)}`}
                </span>
                <span className="row-actions">
                  {j.status === "done" && (
                    <a className="button quiet" href={exportUrl(j.id, "mono")} download>
                      下载
                    </a>
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
    </main>
  );
}
