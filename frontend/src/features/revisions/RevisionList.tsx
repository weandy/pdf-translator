/**
 * 手动修改记录：每次保存修改、重翻、精修、撤回各一条。撤回某一步 = 这一段恢复到那一步之前，
 * 这一段在那之后的修改一并作废；不同段落互不影响（后端 revert_revision）。
 */
import { useMemo, useState } from "react";

import type { Revision } from "@/shared/api/client";
import { diffPreview, diffText, type TextChange } from "@/shared/text/diff";

import "./revisions.css";

const ACTIONS: Record<Revision["action"], string> = {
  edit: "手动修改",
  retranslate: "重翻",
  careful: "精修",
  revert: "撤回",
};

interface Props {
  revisions: Revision[];
  /** 段落 id → 简短说明（如“第 3 页：Large Language Models…”）；只看一段的记录时不传 */
  labels?: Record<string, string>;
  onRevert: (rev: Revision) => Promise<void>;
  onOpen?: (segmentId: string) => void;
}

function MarkedText({ changes, side }: { changes: TextChange[]; side?: "before" | "after" }) {
  return changes.map((change, index) => {
    if (change.kind === "removed")
      return side === "after" ? null : (
        <del key={index} className="revision-removed">
          {change.text}
        </del>
      );
    if (change.kind === "added")
      return side === "before" ? null : (
        <ins key={index} className="revision-added">
          {change.text}
        </ins>
      );
    return <span key={index}>{change.text}</span>;
  });
}

function RevisionDiff({ before, after }: Pick<Revision, "before" | "after">) {
  const diff = useMemo(() => diffText(before, after), [before, after]);
  const changed = diff.changes.some((change) => change.kind !== "equal");
  return (
    <>
      <p className="revision-change-label">{changed ? "变化预览" : "译文内容未变化"}</p>
      {changed && (
        <p className="revision-preview" aria-label="译文变化预览">
          <MarkedText changes={diffPreview(diff.changes)} />
        </p>
      )}
      <details className="revision-comparison">
        <summary>展开完整前后对比</summary>
        {diff.coarse && <p className="hint">段落较长，按整块标记变化。</p>}
        <div className="rev-diff revision-comparison-text">
          <div>
            <span className="revision-comparison-label">修改前</span>
            <p aria-label="修改前译文">{before ? <MarkedText changes={diff.changes} side="before" /> : "（空）"}</p>
          </div>
          <div>
            <span className="revision-comparison-label">修改后</span>
            <p aria-label="修改后译文">{after ? <MarkedText changes={diff.changes} side="after" /> : "（空）"}</p>
          </div>
        </div>
      </details>
    </>
  );
}

const time = (iso: string) =>
  new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function RevisionList({ revisions, labels, onRevert, onOpen }: Props) {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (revisions.length === 0) return <p className="muted revisions-empty">还没有手动修改。</p>;

  const revert = async (rev: Revision) => {
    const later = revisions.filter(
      (r) => r.segment_id === rev.segment_id && r.id > rev.id && r.action !== "revert" && r.reverted_by === null,
    ).length;
    if (later > 0 && !window.confirm(`这一段在这一步之后还有 ${later} 次修改，会一并撤回。继续吗？`)) return;
    setBusy(rev.id);
    setError(null);
    try {
      await onRevert(rev);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      {error && <p className="error">{error}</p>}
      <p className="revision-legend">
        <ins className="revision-added">新增或替换</ins>用粗体、浅底和点状下划线标记，
        <del className="revision-removed">删除内容</del>用删除线标记。
      </p>
      <ol className="revisions">
        {[...revisions].reverse().map((rev) => {
          const voided = rev.reverted_by !== null;
          return (
            <li key={rev.id} className={voided ? "voided" : undefined}>
              <div className="rev-head">
                <span className={`tag ${rev.action === "revert" ? "" : "edited"}`}>{ACTIONS[rev.action]}</span>
                <span className="muted num">{time(rev.created_at)}</span>
                {voided && <span className="muted">已撤回</span>}
                <span className="spacer" />
                {rev.action !== "revert" && !voided && (
                  <button
                    type="button"
                    className="quiet"
                    disabled={busy !== null}
                    onClick={() => void revert(rev)}
                    title="这一段恢复到这一步之前的译文"
                  >
                    {busy === rev.id ? "撤回中…" : "撤回这一步"}
                  </button>
                )}
              </div>
              {labels && (
                <button type="button" className="link rev-target" onClick={() => onOpen?.(rev.segment_id)}>
                  {labels[rev.segment_id] ?? "段落"}
                </button>
              )}
              <RevisionDiff before={rev.before} after={rev.after} />
            </li>
          );
        })}
      </ol>
    </>
  );
}
