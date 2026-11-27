/**
 * 任务页：进行中显示进度，完成后显示阅读器；阅读器、下载与段落编辑在这里组合（功能模块之间互不依赖）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useOutletContext, useParams } from "react-router";

import { progressOf, withSegment } from "@/entities/document/model";
import { STAGE_LABELS, useJob } from "@/entities/job/model";
import { savePrefs, useSettings } from "@/entities/settings/model";
import { ExpandPagesDialog } from "@/features/expansion/ExpandPagesDialog";
import { ExpansionBanner } from "@/features/expansion/ExpansionBanner";
import { useExpansion } from "@/features/expansion/useExpansion";
import { ExportDownloadLink } from "@/features/export/ExportDownloadLink";
import { DownloadMenu } from "@/features/export/DownloadMenu";
import { TermsPanel } from "@/features/job-terms/TermsPanel";
import { JobProgress } from "@/features/job-progress/JobProgress";
import { DisplayMenu } from "@/features/reader/DisplayMenu";
import { ReaderView, type LocatePage } from "@/features/reader/ReaderView";
import { RevisionList } from "@/features/revisions/RevisionList";
import { SegmentEditor } from "@/features/segment-edit/SegmentEditor";
import {
  api,
  fileUrl,
  type DocumentData,
  type Job,
  type Revision,
  type SegmentData,
} from "@/shared/api/client";
import { APP_TITLE, notify, useVisible } from "@/shared/attention/attention";
import { hasOpenModal, useConfirm } from "@/shared/feedback/ConfirmDialog";
import { ErrorCard } from "@/shared/feedback/ErrorCard";
import { useTheme } from "@/shared/theme/theme";
import { expiresSoon, formatRemaining } from "@/shared/time/elapsed";

interface JobOutletContext {
  openSettings?: () => void;
}

export function JobPage() {
  const { id = "" } = useParams();
  const location = useLocation();
  const outlet = useOutletContext<JobOutletContext | null>();
  const { resolved: theme, lightPaper, darkPaper, setPreference: setTheme, setLightPaper, setDarkPaper } = useTheme();
  const confirm = useConfirm();
  const [request, setLocateRequest] = useState<{ key: string; target: LocatePage } | null>(null);
  const { job, error, refresh } = useJob(id);
  const { active, started } = useExpansion(job, id, refresh);
  const [expanding, setExpanding] = useState<string | null>(null);
  const contentVersion = job?.content_version ?? 1;
  useJobTitle(job);
  const qc = useQueryClient();
  const { translationSide } = useSettings();
  /** 纯阅读态下轻聚焦的段落（只高亮 + 浮现「校对译文」胶囊，不弹出 400px 抽屉） */
  const [focused, setFocused] = useState<string | null>(null);
  /** 正式打开右侧段落编辑抽屉的段落 */
  const [selected, setSelected] = useState<string | null>(null);
  const editorDirtyRef = useRef(false);
  const setEditorDirty = useCallback((dirty: boolean) => {
    editorDirtyRef.current = dirty;
  }, []);
  const historyButton = useRef<HTMLButtonElement>(null);
  const termsButton = useRef<HTMLButtonElement>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showTerms, setShowTerms] = useState(false);
  /** 从「本文术语」临时切去改一段时保持术语面板挂起状态，关编辑后无缝回到原术语定位 */
  const [returnToTerms, setReturnToTerms] = useState(false);
  const [reviewingFailed, setReviewingFailed] = useState(false);
  const [spot, setSpot] = useState<{ id: string; n: number; term?: { source: string; target: string } } | null>(null);
  const [marked, setMarked] = useState<ReadonlySet<string>>(new Set());
  const [revealKey, setRevealKey] = useState(0);
  useEffect(() => {
    if (!showHistory && !showTerms && !focused) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.isComposing || e.defaultPrevented || hasOpenModal()) return;
      if (showHistory) {
        e.preventDefault();
        setShowHistory(false);
        historyButton.current?.focus();
      } else if (showTerms) {
        e.preventDefault();
        setShowTerms(false);
        setReturnToTerms(false);
        setSpot(null);
        setMarked(new Set());
        termsButton.current?.focus();
      } else if (focused && !selected) {
        e.preventDefault();
        setFocused(null);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showHistory, showTerms, focused, selected]);
  const ready = job?.status === "done";
  const {
    data: queriedDoc,
    error: docError,
    isFetching: documentLoading,
  } = useQuery({
    queryKey: ["document", id, contentVersion],
    queryFn: ({ signal }) => api.document(id, signal),
    placeholderData: (previous) => previous?.job.id === id ? previous : undefined,
    enabled: ready,
    // 超时后的自动重试可能与服务端尚未结束的计算重叠，交给用户手动重新打开。
    retry: false,
  });
  const { data: revisions = [] } = useQuery({
    queryKey: ["revisions", id, contentVersion],
    queryFn: () => api.revisions(id),
    placeholderData: (previous, query) => query?.queryKey[1] === id ? previous : undefined,
    enabled: ready,
  });
  const doc = queriedDoc ?? (docError
    ? qc.getQueriesData<DocumentData>({ queryKey: ["document", id] })
        .flatMap(([, value]) => value ? [value] : [])
        .sort((left, right) => right.job.content_version - left.job.content_version)[0]
    : undefined);
  const labels = useMemo(() => segmentLabels(doc), [doc]);

  if (error)
    return (
      <main className="center">
        <ErrorCard title="无法打开任务" message={error} actionLabel="重新加载" onAction={refresh} />
      </main>
    );
  if (!job) return <main className="center muted">正在打开…</main>;
  if (!ready) return <JobProgress key={id} job={job} onRetried={refresh} />;
  if (!doc)
    return (
      <DocumentOpening
        key={id}
        jobId={id}
        side={translationSide}
        failed={Boolean(docError) && !documentLoading}
        onRetry={() => void qc.invalidateQueries({ queryKey: ["document", id] })}
      />
    );

  const displayedVersion = doc.job.content_version ?? 1;
  const readOnly = Boolean(active) || displayedVersion !== contentVersion;
  const pages = job.published_pages ?? job.options.pages;
  const publishedCount = pages?.length ?? job.page_count;
  const queryPage = new URLSearchParams(location.search).get("page");
  const directPage = queryPage && /^[1-9]\d*$/.test(queryPage) ? Number(queryPage) : 0;
  const locatePage: LocatePage | null = request?.key === location.key
    ? request.target
    : doc.pages.some((page) => page.index === directPage - 1)
      ? { page: directPage, seq: 0, highlight: true }
      : null;
  const showPage = (page: number) => {
    setLocateRequest((previous) => ({
      key: location.key,
      target: { page, seq: (previous?.target.seq ?? 0) + 1, highlight: true },
    }));
  };

  const onChange = (seg: SegmentData) => {
    qc.setQueryData<DocumentData>(["document", id, contentVersion], (d) => (d ? withSegment(d, seg) : d));
    void qc.invalidateQueries({ queryKey: ["revisions", id, contentVersion] });
    // 重翻会更新任务费用，编辑或重翻结束后立即刷新任务摘要，顶部费用与保留时间保持同步。
    refresh();
  };
  const onRevert = async (rev: Revision) => onChange(await api.revert(rev.id));
  const onRestore = async (rev: Revision, target: string) => onChange(await api.editSegment(rev.segment_id, target));
  const currentTargets = Object.fromEntries(doc.segments.map((s) => [s.id, s.target] as const));
  const guardDirty = (action: () => void) => {
    if (!editorDirtyRef.current) {
      action();
      return;
    }
    void confirm({
      title: "放弃未保存的修改？",
      message: "当前段落的译文修改尚未保存，离开后这些改动将丢失。",
      confirmLabel: "放弃修改",
      cancelLabel: "继续编辑",
      tone: "danger",
    }).then((accepted) => {
      if (!accepted) return;
      editorDirtyRef.current = false;
      action();
    });
  };
  const open = (segmentId: string, options?: { keepTerms?: boolean }) => {
    guardDirty(() => {
      const preserveTerms = Boolean(options?.keepTerms || showTerms || returnToTerms);
      setReviewingFailed(false);
      setFocused(segmentId);
      setSelected(segmentId);
      void qc.invalidateQueries({ queryKey: ["document", id, contentVersion] });
      setShowHistory(false);
      if (preserveTerms) {
        setReturnToTerms(true);
        setShowTerms(false);
      } else {
        setShowTerms(false);
        setMarked(new Set());
      }
    });
  };
  // 纯阅读态单击段落只做轻聚焦 + 浮现「校对译文」胶囊，避免误触挤出 400px 抽屉导致页面惊跳；
  // 一旦已处于精校态（右侧编辑抽屉打开中），单击其它段落直接切换编辑对象。
  const selectSegment = (segmentId: string) => {
    if (selected !== null) {
      if (segmentId !== selected) open(segmentId);
      return;
    }
    setFocused(segmentId);
  };
  const editSegment = (segmentId: string) => open(segmentId);
  const editFromTerms = (segmentId: string) => {
    open(segmentId, { keepTerms: true });
    setRevealKey((k) => k + 1);
  };
  const backToTerms = () => {
    guardDirty(() => {
      setSelected(null);
      setReviewingFailed(false);
      setShowTerms(true);
      setReturnToTerms(false);
    });
  };
  // 关闭编辑后：若从「本文术语」进入则返回术语面板，否则把焦点还给原来的段落（键盘用户接着 Tab）
  const closeEditor = () => {
    guardDirty(() => {
      const id = selected;
      setSelected(null);
      setReviewingFailed(false);
      if (returnToTerms) {
        setShowTerms(true);
        setReturnToTerms(false);
        return;
      }
      requestAnimationFrame(() => document.querySelector<HTMLElement>(`[data-seg="${id}"]`)?.focus());
    });
  };
  const reveal = (segmentId: string) => {
    open(segmentId, { keepTerms: false });
    setRevealKey((k) => k + 1);
  };
  const changes = revisions.filter((r) => r.action !== "revert" && r.reverted_by === null).length;
  const segmentIndex = doc.segments.findIndex((s) => s.id === selected);
  const segment = segmentIndex >= 0 ? (doc.segments[segmentIndex] ?? null) : null;
  const stepSegment = (delta: -1 | 1) => {
    const next = doc.segments[segmentIndex + delta];
    if (!next) return;
    open(next.id);
    setRevealKey((k) => k + 1);
  };
  const stats = progressOf(doc);
  // 沿用解析器的段落阅读顺序，避免按坐标排序打乱多栏正文。
  const failedSegments = doc.segments.filter((s) => s.status === "failed");
  const failedIndex = failedSegments.findIndex((s) => s.id === selected);
  const locateFailed = (segmentId: string) => {
    guardDirty(() => {
      setFocused(segmentId);
      setSelected(segmentId);
      setReviewingFailed(true);
      setShowHistory(false);
      setShowTerms(false);
      setReturnToTerms(false);
      setMarked(new Set());
      void qc.invalidateQueries({ queryKey: ["document", id, contentVersion] });
      setSpot((s) => ({ id: segmentId, n: (s?.n ?? 0) + 1 }));
    });
  };
  const moveFailed = (delta: number) => {
    if (!failedSegments.length) return;
    // 当前段修好后，以它在完整文档中的位置找下一段，不跳过紧接的问题段落。
    const selectedIndex = doc.segments.findIndex((s) => s.id === selected);
    const next =
      failedIndex >= 0
        ? failedSegments[(failedIndex + delta + failedSegments.length) % failedSegments.length]
        : (doc.segments.slice(selectedIndex + 1).find((s) => s.status === "failed") ?? failedSegments[0]);
    if (next) locateFailed(next.id);
  };

  const docTooltipParts = [doc.title || job.filename];
  if (job.cost_cny !== null) docTooltipParts.push(`花费 ¥${job.cost_cny.toFixed(3)}`);
  if (job.expires_at) docTooltipParts.push(`服务器保留${formatRemaining(job.expires_at)}`);
  const hasHighSignalMeta =
    publishedCount < job.page_count ||
    Boolean(active) ||
    stats.failed > 0 ||
    Boolean(job.expires_at && expiresSoon(job.expires_at));
  const rightPanelOpen = Boolean(showHistory || showTerms || (!readOnly && segment));
  const termsMounted = showTerms || returnToTerms;

  const heading = (
    <div className="toolbar-lead">
      <Link to="/" className="back" aria-label="返回首页">
        <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        <span>返回</span>
      </Link>
      <div className="doc-heading">
        <span className="doc-title" title={docTooltipParts.join(" · ")}>
          {doc.title || job.filename}
        </span>
        {hasHighSignalMeta && (
          <span className="doc-meta num">
            {publishedCount < job.page_count && <span>仅翻译了 {publishedCount}/{job.page_count} 页</span>}
            {active && <span>正在补翻其余页</span>}
            {stats.failed > 0 && (
              <button
                type="button"
                className="link failed-segments-link"
                disabled={readOnly}
                onClick={() => locateFailed(failedSegments[failedIndex >= 0 ? failedIndex : 0]!.id)}
                title="定位问题段落并打开编辑面板，可重翻或手动修改"
              >
                {stats.failed} 段未完成翻译 · 点击定位
              </button>
            )}
            {job.expires_at && expiresSoon(job.expires_at) && (
              <span className="error">保留{formatRemaining(job.expires_at)}，请先下载</span>
            )}
          </span>
        )}
      </div>
    </div>
  );

  return (
    <main className="job">
      {active && <ExpansionBanner key={active.id} op={active} />}
      {expanding === id && !readOnly && (
        <ExpandPagesDialog
          job={job}
          published={new Set(pages ?? [])}
          onClose={() => setExpanding(null)}
          onStarted={(op) => {
            setExpanding(null);
            setSelected(null);
            started(op);
          }}
        />
      )}
      {docError && displayedVersion !== contentVersion && (
        <button type="button" onClick={() => void qc.invalidateQueries({ queryKey: ["document", id, contentVersion] })}>
          重新加载补翻后的译文
        </button>
      )}
      <div className="workspace">
        <ReaderView
          key={id}
          doc={doc}
          jobId={id}
          locatePage={locatePage}
          selectedSegment={selected ?? focused}
          editingOpen={Boolean(!readOnly && !showHistory && !showTerms && segment)}
          onSelectSegment={readOnly ? undefined : selectSegment}
          onEditSegment={readOnly ? undefined : editSegment}
          onClearSelection={() => setFocused(null)}
          backgroundUrl={fileUrl(id, "background.pdf", displayedVersion)}
          heading={heading}
          side={translationSide}
          revealKey={revealKey}
          spot={spot}
          marked={marked}
          rightPanelOpen={rightPanelOpen}
          actions={
            <>
              <button
                type="button"
                className={showTerms ? "quiet toolbar-btn on" : "quiet toolbar-btn"}
                ref={termsButton}
                aria-pressed={showTerms}
                onClick={() => {
                  guardDirty(() => {
                    if (showTerms) {
                      setShowTerms(false);
                      setReturnToTerms(false);
                      setSpot(null);
                      setMarked(new Set());
                    } else {
                      setShowTerms(true);
                      setReturnToTerms(false);
                      setShowHistory(false);
                      setSelected(null);
                      setReviewingFailed(false);
                    }
                  });
                }}
                title="这篇用到的术语，检查译法是否一致"
              >
                <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
                </svg>
                <span>术语</span>
              </button>
              <button
                type="button"
                className={showHistory ? "quiet toolbar-btn on" : "quiet toolbar-btn"}
                ref={historyButton}
                aria-pressed={showHistory}
                onClick={() => {
                  guardDirty(() => {
                    setShowHistory((v) => !v);
                    setShowTerms(false);
                    setReturnToTerms(false);
                    setSpot(null);
                    setMarked(new Set());
                  });
                }}
                title="所有手动修改，可按步撤回"
              >
                <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
                  <polyline points="1 4 1 10 7 10" />
                  <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                </svg>
                <span>
                  <span className="wide-only">修改</span>记录{changes > 0 && <span className="num"> {changes}</span>}
                </span>
              </button>
              {!readOnly && publishedCount < job.page_count && (
                <button type="button" className="quiet toolbar-btn" onClick={() => setExpanding(id)}>
                  补翻其余页
                </button>
              )}
              <span className="toolbar-sep" aria-hidden="true" />
              <DisplayMenu
                theme={theme}
                lightPaper={lightPaper}
                darkPaper={darkPaper}
                side={translationSide}
                onThemeChange={setTheme}
                onLightPaperChange={setLightPaper}
                onDarkPaperChange={setDarkPaper}
                onSideChange={(nextSide) => savePrefs({ translationSide: nextSide })}
                onOpenSettings={outlet?.openSettings}
              />
              <DownloadMenu jobId={id} contentVersion={contentVersion} onLocatePage={showPage} />
            </>
          }
        />
        {showHistory && (
          <aside className="editor" aria-label="修改记录">
            <span className="editor-grip" aria-hidden="true" />
            <header>
              <strong>修改记录</strong>
              <span className="spacer" />
              <button type="button" className="quiet" onClick={() => setShowHistory(false)}>
                关闭
              </button>
            </header>
            <p className="hint">点段落名称可打开那一段；撤回后仍可随时恢复任意历史版本或撤销撤回。</p>
            <RevisionList
              revisions={revisions}
              labels={labels}
              currentTargets={currentTargets}
              onRevert={onRevert}
              onRestore={onRestore}
              onOpen={reveal}
              readOnly={readOnly}
            />
          </aside>
        )}
        {termsMounted && (
          <TermsPanel
            jobId={id}
            doc={doc}
            channelMode={job.channel_mode}
            readOnly={readOnly}
            hidden={!showTerms}
            onChange={onChange}
            onSpot={(segmentId, term) => setSpot((s) => ({ id: segmentId, n: (s?.n ?? 0) + 1, term }))}
            onMark={(ids) => setMarked(new Set(ids))}
            onEditSegment={readOnly ? undefined : editFromTerms}
            onClose={() => {
              setShowTerms(false);
              setReturnToTerms(false);
              setSpot(null);
              setMarked(new Set());
            }}
          />
        )}
        {!readOnly && !showHistory && !showTerms && segment && (
          <SegmentEditor
            key={`${segment.id}:${segment.target ?? ""}:${Object.entries(segment.formulas)
              .map(([k, v]) => `${k}=${v.latex}`)
              .join("|")}`}
            segment={segment}
            channelMode={job.channel_mode}
            onChange={onChange}
            onClose={closeEditor}
            onDirtyChange={setEditorDirty}
            onStep={stepSegment}
            canStepPrev={segmentIndex > 0}
            canStepNext={segmentIndex >= 0 && segmentIndex < doc.segments.length - 1}
            returnToTerms={returnToTerms}
            onReturnToTerms={backToTerms}
            navigation={
              reviewingFailed && (
                <nav className="failed-segment-nav" aria-label="待处理段落导航">
                  <span role="status">
                    {failedIndex >= 0
                      ? `待处理 ${failedIndex + 1} / ${failedSegments.length}`
                      : failedSegments.length > 0
                        ? `本段已处理，还剩 ${failedSegments.length} 段`
                        : "待处理段落已全部处理"}
                  </span>
                  {failedSegments.length > 1 && failedIndex >= 0 && (
                    <button type="button" className="quiet" onClick={() => moveFailed(-1)}>
                      上一处
                    </button>
                  )}
                  {failedSegments.length > 0 && (failedSegments.length > 1 || failedIndex < 0) && (
                    <button type="button" className="quiet" onClick={() => moveFailed(1)}>
                      {failedIndex >= 0 ? "下一处" : "查看下一处"}
                    </button>
                  )}
                </nav>
              )
            }
            history={
              <RevisionList
                revisions={revisions.filter((r) => r.segment_id === segment.id)}
                currentTargets={currentTargets}
                onRevert={onRevert}
                onRestore={onRestore}
                readOnly={readOnly}
              />
            }
          />
        )}
      </div>
    </main>
  );
}

function DocumentOpening({
  jobId,
  side,
  failed,
  onRetry,
}: {
  jobId: string;
  side: "left" | "right";
  failed: boolean;
  onRetry: () => void;
}) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (failed) return;
    const timer = setTimeout(() => setSlow(true), 8000);
    return () => clearTimeout(timer);
  }, [failed]);
  return (
    <main className="center document-opening">
      <Link to="/" className="back">
        返回首页
      </Link>
      <h1>翻译已完成</h1>
      {failed ? (
        <ErrorCard
          title="暂时无法打开阅读器"
          message="阅读数据未能及时加载，翻译任务已经完成。请稍后重新打开，或尝试下载 PDF。"
          actionLabel="重新打开译文"
          onAction={onRetry}
        />
      ) : (
        <div role="status" aria-live="polite">
          <p>正在打开译文…</p>
          {slow && <p className="hint">首次整理文档链接可能稍慢，你也可以先尝试下载 PDF。</p>}
        </div>
      )}
      <nav className="document-opening-downloads" aria-label="下载已完成的文档">
        <div>
          <ExportDownloadLink className="button" jobId={jobId} mode="mono">
            下载中文版
          </ExportDownloadLink>
        </div>
        <div>
          <ExportDownloadLink className="button" jobId={jobId} mode="dual" side={side}>
            下载双语版
          </ExportDownloadLink>
        </div>
        <a className="button" href={fileUrl(jobId, "original.pdf")} download>
          下载原文
        </a>
      </nav>
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
