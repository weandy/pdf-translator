import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  deleteJob,
  MY_JOBS_QUERY,
  STAGE_LABELS,
  useMyJobs,
} from "@/entities/job/model";
import { UploadPanel } from "@/features/upload/UploadPanel";

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
    if (!window.confirm(`删除“${filename}”的原文与译文？删除后无法恢复。`))
      return;
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
      <h1>PDF 保版式翻译</h1>
      <p className="lead">
        上传英文论文 PDF，按原版式对照阅读中文译文，公式与图表保持原样。
      </p>
      <UploadPanel
        onCreated={(job) => void navigate(`/jobs/${job.id}`)}
        onNeedSettings={openSettings}
      />
      {jobs && jobs.length > 0 && (
        <section className="recent">
          <h2>我的任务</h2>
          {error && <p className="error">{error}</p>}
          <ul>
            {jobs.map((j) => (
              <li key={j.id}>
                <Link to={`/jobs/${j.id}`}>{j.filename}</Link>
                <span className={`badge ${j.status}`}>
                  {STATUS[j.status]}
                  {j.status === "running" &&
                    ` · ${STAGE_LABELS[j.stage] ?? j.stage}`}
                </span>
                <span className="muted">
                  {j.page_count} 页
                  {j.cost_cny !== null && ` · ¥${j.cost_cny.toFixed(3)}`}
                </span>
                {j.status !== "running" && j.status !== "queued" && (
                  <button
                    type="button"
                    className="link"
                    onClick={() => void remove(j.id, j.filename)}
                  >
                    删除
                  </button>
                )}
              </li>
            ))}
          </ul>
          <p className="hint">只显示在这个浏览器里创建的任务。</p>
        </section>
      )}
    </main>
  );
}
