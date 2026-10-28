/**
 * 任务页：进行中显示进度，完成后显示阅读器；阅读器与段落编辑在这里组合（功能模块之间互不依赖）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { progressOf, withSegment } from "@/entities/document/model";
import { useJob } from "@/entities/job/model";
import { JobProgress } from "@/features/job-progress/JobProgress";
import { ReaderView } from "@/features/reader/ReaderView";
import { SegmentEditor } from "@/features/segment-edit/SegmentEditor";
import { api, type DocumentData, type SegmentData } from "@/shared/api/client";

export function JobPage() {
  const { id = "" } = useParams();
  const { job, error, refresh } = useJob(id);
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const ready = job?.status === "done";
  const { data: doc, error: docError } = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.document(id),
    enabled: ready,
  });

  if (error) return <main className="center error">{error}</main>;
  if (!job) return <main className="center muted">加载中…</main>;

  const onChange = (seg: SegmentData) =>
    qc.setQueryData<DocumentData>(["document", id], (d) => (d ? withSegment(d, seg) : d));
  const segment = doc?.segments.find((s) => s.id === selected) ?? null;
  const stats = doc ? progressOf(doc) : null;

  return (
    <main className="job">
      <div className="job-header">
        <Link to="/">← 返回</Link>
        <strong className="title">{doc?.title || job.filename}</strong>
        {stats && (
          <span className="muted">
            {stats.done}/{stats.total} 段
            {stats.failed > 0 && <span className="error"> · {stats.failed} 段失败，点击段落可重试</span>}
          </span>
        )}
        {job.cost_cny !== null && <span className="muted">费用 ¥{job.cost_cny.toFixed(3)}</span>}
        {job.message && ready && <span className="muted">{job.message}</span>}
      </div>
      {!ready && <JobProgress job={job} onRetried={refresh} />}
      {docError && <p className="error">{String(docError)}</p>}
      {doc && (
        <div className="workspace">
          <ReaderView doc={doc} jobId={id} selectedSegment={selected} onSelectSegment={setSelected} />
          {segment && (
            <SegmentEditor
              key={`${segment.id}:${segment.target ?? ""}`}
              segment={segment}
              onChange={onChange}
              onClose={() => setSelected(null)}
            />
          )}
        </div>
      )}
    </main>
  );
}
