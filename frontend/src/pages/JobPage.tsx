/**
 * 任务页：进行中显示进度，完成后显示阅读器；阅读器、下载与段落编辑在这里组合（功能模块之间互不依赖）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { progressOf, withSegment } from "@/entities/document/model";
import { STAGE_LABELS, useJob } from "@/entities/job/model";
import { useSettings } from "@/entities/settings/model";
import { DownloadMenu } from "@/features/export/DownloadMenu";
import { JobProgress } from "@/features/job-progress/JobProgress";
import { ReaderView } from "@/features/reader/ReaderView";
import { RevisionList } from "@/features/revisions/RevisionList";
import { SegmentEditor } from "@/features/segment-edit/SegmentEditor";
import { api, type DocumentData, type Job, type Revision, type SegmentData } from "@/shared/api/client";
import { APP_TITLE, notify, useVisible } from "@/shared/attention/attention";
import { expiresSoon, formatRemaining } from "@/shared/time/elapsed";

export function JobPage() {
  const { id = "" } = useParams();
  const { job, error, refresh } = useJob(id);
  useJobTitle(job);
  const qc = useQueryClient();
  const { translationSide } = useSettings();
  const [selected, setSelected] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [revealKey, setRevealKey] = useState(0);
  useEffect(() => {
    if (!showHistory) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setShowHistory(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showHistory]);
  const ready = job?.status === "done";
  const { data: doc, error: docError } = useQuery({
    queryKey: ["document", id],
    queryFn: () => api.document(id),
    enabled: ready,
  });
  const { data: revisions = [] } = useQuery({
    queryKey: ["revisions", id],
    queryFn: () => api.revisions(id),
    enabled: ready,
  });
  const labels = useMemo(() => segmentLabels(doc), [doc]);

  if (error) return <main className="center error">{error}</main>;
  if (!job) return <main className="center muted">正在打开…</main>;
  if (!ready) return <JobProgress job={job} onRetried={refresh} />;
  if (docError) return <main className="center error">{String(docError)}</main>;
  if (!doc) return <main className="center muted">正在打开译文…</main>;

  const onChange = (seg: SegmentData) => {
    qc.setQueryData<DocumentData>(["document", id], (d) => (d ? withSegment(d, seg) : d));
    void qc.invalidateQueries({ queryKey: ["revisions", id] });
  };
  const onRevert = async (rev: Revision) => onChange(await api.revert(rev.id));
  const open = (segmentId: string) => {
    setSelected(segmentId);
    setShowHistory(false);
  };
  // 关闭编辑后把焦点还给原来的段落（键盘用户接着 Tab）
  const closeEditor = () => {
    const id = selected;
    setSelected(null);
    requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-seg="${id}"]`)?.focus());
  };
  const reveal = (segmentId: string) => {
    open(segmentId);
    setRevealKey((k) => k + 1);
  };
  const changes = revisions.filter((r) => r.action !== "revert" && r.reverted_by === null).length;
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
          {job.expires_at &&
            (expiresSoon(job.expires_at) ? (
              <span className="error">服务器保留{formatRemaining(job.expires_at)}，到期自动删除，请先下载</span>
            ) : (
              <span title="到期后原文与译文自动删除">服务器保留{formatRemaining(job.expires_at)}</span>
            ))}
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
          onSelectSegment={open}
          heading={heading}
          side={translationSide}
          revealKey={revealKey}
          actions={
            <>
              <button
                type="button"
                className={showHistory ? "quiet on" : "quiet"}
                aria-pressed={showHistory}
                onClick={() => setShowHistory((v) => !v)}
                title="所有手动修改，可按步撤回"
              >
                <span>
                  <span className="wide-only">修改</span>记录{changes > 0 && <span className="num"> {changes}</span>}
                </span>
              </button>
              <DownloadMenu jobId={id} />
            </>
          }
        />
        {showHistory && (
          <aside className="editor" aria-label="修改记录">
            <header>
              <strong>修改记录</strong>
              <span className="spacer" />
              <button type="button" className="quiet" onClick={() => setShowHistory(false)}>
                关闭
              </button>
            </header>
            <p className="hint">点段落名称可打开那一段；撤回某一步，这一段会恢复到那一步之前的译文。</p>
            <RevisionList revisions={revisions} labels={labels} onRevert={onRevert} onOpen={reveal} />
          </aside>
        )}
        {!showHistory && segment && (
          <SegmentEditor
            key={`${segment.id}:${segment.target ?? ""}`}
            segment={segment}
            onChange={onChange}
            onClose={closeEditor}
            history={
              <RevisionList revisions={revisions.filter((r) => r.segment_id === segment.id)} onRevert={onRevert} />
            }
          />
        )}
      </div>
    </main>
  );
}

/**
 * 标签标题：进行中显示阶段与进度；页面在后台时完成或失败，标题显示 ✓ / ✗ 并发桌面通知（用户同意过时），
 * 回到这个标签后恢复为文件名。
 */
function useJobTitle(job: Job | null): void {
  const visible = useVisible();
  const last = useRef<string | null>(null);
  const unseen = useRef(false);
  useEffect(() => {
    if (!job) return;
    const live = job.status === "queued" || job.status === "running";
    const wasLive = last.current === "queued" || last.current === "running";
    last.current = job.status;
    if (wasLive && !live) {
      unseen.current = !visible;
      if (job.status === "done") notify("翻译完成", job.filename);
      if (job.status === "failed") notify("翻译失败", job.filename);
    }
    if (visible) unseen.current = false;
    // 只有翻译阶段的进度是真实完成比例（解析阶段的进度条先是上传进度，传完还要等云端解析）
    const pct = job.stage === "translate" ? ` ${Math.round(job.progress * 100)}%` : "";
    document.title =
      job.status === "queued"
        ? `排队中 · ${job.filename}`
        : job.status === "running"
          ? `${STAGE_LABELS[job.stage] ?? "处理中"}${pct} · ${job.filename}`
          : unseen.current
            ? `${job.status === "done" ? "✓ 已完成" : job.status === "failed" ? "✗ 失败" : "已取消"} · ${job.filename}`
            : job.filename;
  }, [job, visible]);
  useEffect(
    () => () => {
      document.title = APP_TITLE;
    },
    [],
  );
}

/** 段落 id → “第 3 页：原文开头…”，修改记录里用来指明是哪一段。 */
function segmentLabels(doc: DocumentData | undefined): Record<string, string> {
  if (!doc) return {};
  const pageOf = new Map(doc.pages.flatMap((p) => p.blocks.map((b) => [b.id, p.index] as const)));
  return Object.fromEntries(
    doc.segments.map((s) => {
      const text = s.source.replace(/<m\d+\/>/g, "…").trim();
      const page = pageOf.get(s.block_ids[0] ?? "");
      return [
        s.id,
        `${page === undefined ? "" : `第 ${page + 1} 页：`}${text.slice(0, 40)}${text.length > 40 ? "…" : ""}`,
      ];
    }),
  );
}
