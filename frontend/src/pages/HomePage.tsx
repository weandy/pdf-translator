import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";

import { STAGE_LABELS } from "@/entities/job/model";
import { UploadPanel } from "@/features/upload/UploadPanel";
import { api } from "@/shared/api/client";

const STATUS: Record<string, string> = { queued: "排队中", running: "进行中", done: "已完成", failed: "失败" };

export function HomePage({ openSettings }: { openSettings: () => void }) {
  const navigate = useNavigate();
  const { data: jobs } = useQuery({ queryKey: ["jobs"], queryFn: api.recentJobs });

  return (
    <main className="home">
      <h1>PDF 保版式翻译</h1>
      <p className="lead">上传英文论文 PDF，按原版式对照阅读中文译文，公式与图表保持原样。</p>
      <UploadPanel onCreated={(job) => void navigate(`/jobs/${job.id}`)} onNeedSettings={openSettings} />
      {jobs && jobs.length > 0 && (
        <section className="recent">
          <h2>最近的任务</h2>
          <ul>
            {jobs.map((j) => (
              <li key={j.id}>
                <Link to={`/jobs/${j.id}`}>{j.filename}</Link>
                <span className={`badge ${j.status}`}>
                  {STATUS[j.status]}
                  {j.status === "running" && ` · ${STAGE_LABELS[j.stage] ?? j.stage}`}
                </span>
                <span className="muted">
                  {j.page_count} 页{j.cost_cny !== null && ` · ¥${j.cost_cny.toFixed(3)}`}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
