/**
 * 任务页：进行中显示进度，完成后显示阅读器；阅读器、下载与段落编辑在这里组合（功能模块之间互不依赖）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { progressOf, withSegment } from "@/entities/document/model";
import { useJob } from "@/entities/job/model";
import { useSettings } from "@/entities/settings/model";
import { DownloadMenu } from "@/features/export/DownloadMenu";
import { JobProgress } from "@/features/job-progress/JobProgress";
import { ReaderView } from "@/features/reader/ReaderView";
import { SegmentEditor } from "@/features/segment-edit/SegmentEditor";
import { api, type DocumentData, type SegmentData } from "@/shared/api/client";

export function JobPage() {
  const { id = "" } = useParams();
  const { job, error, refresh } = useJob(id);
  const qc = useQueryClient();
  const { translationSide } = useSettings();
  const [selected, setSelected] = useState<string | null>(null);
  const ready = job?.status === "done";
  const { data: doc, error: docError } = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.document(id),
    enabled: ready,
  });

  if (error) return <main className="center error">{error}</main>;
  if (!job) return <main className="center muted">正在打开…</main>;
  if (!ready) return <JobProgress job={job} onRetried={refresh} />;
  if (docError) return <main className="center error">{String(docError)}</main>;
  if (!doc) return <main className="center muted">正在打开译文…</main>;

  const onChange = (seg: SegmentData) =>
    qc.setQueryData<DocumentData>(["document", id], (d) => (d ? withSegment(d, seg) : d));
  const segment = doc.segments.find((s) => s.id === selected) ?? null;
  const stats = progressOf(doc);

  const heading = (
    <>
      <Link to="/" className="back">
        返回
      </Link>
      <div className="doc-heading">
        <span className="doc-title" title={doc.title || job.filename}>
          {doc.title || job.filename}
        </span>
        <span className="doc-meta num">
          <span>
            已翻译 {stats.done}/{stats.total} 段
          </span>
          {stats.failed > 0 && <span className="error">{stats.failed} 段未通过校验，点击灰色段落重翻</span>}
          {job.cost_cny !== null && <span>花费 ¥{job.cost_cny.toFixed(3)}</span>}
        </span>
      </div>
    </>
  );

  return (
    <main className="job">
      <div className="workspace">
        <ReaderView
          doc={doc}
          jobId={id}
          selectedSegment={selected}
          onSelectSegment={setSelected}
          heading={heading}
          side={translationSide}
          actions={<DownloadMenu jobId={id} />}
        />
        {segment && (
          <SegmentEditor
            key={`${segment.id}:${segment.target ?? ""}`}
            segment={segment}
            onChange={onChange}
            onClose={() => setSelected(null)}
          />
        )}
      </div>
    </main>
  );
}
