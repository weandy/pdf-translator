/**
 * 手动修改记录：每次保存修改、重翻、精修、撤回各一条。撤回某一步 = 这一段恢复到那一步之前，
 * 这一段在那之后的修改一并作废；不同段落互不影响（后端 revert_revision）。
 */
import { useState } from "react";

import type { Revision } from "@/shared/api/client";

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

const clip = (text: string | null, n = 80) => {
  if (!text) return "（空）";
  return text.length > n ? `${text.slice(0, n)}…` : text;
};

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
              <p className="rev-diff">
                <del>{clip(rev.before)}</del>
                <ins>{clip(rev.after)}</ins>
              </p>
            </li>
          );
        })}
      </ol>
    </>
  );
}
