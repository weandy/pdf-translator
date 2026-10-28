import { useState } from "react";

import { api, type Quality, type SegmentData } from "@/shared/api/client";

interface Props {
  segment: SegmentData;
  onChange: (seg: SegmentData) => void;
  onClose: () => void;
}

const STATUS: Record<SegmentData["status"], string> = {
  pending: "未翻译",
  done: "已翻译",
  failed: "翻译失败",
  edited: "已手动修改",
};

/** 切换段落或译文更新时由调用方用 key 重新挂载，草稿从 props 初始化。 */
export function SegmentEditor({ segment, onChange, onClose }: Props) {
  const [draft, setDraft] = useState(segment.target ?? "");
  const [busy, setBusy] = useState<"save" | Quality | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (kind: "save" | Quality) => {
    setBusy(kind);
    setError(null);
    try {
      const seg = kind === "save" ? await api.editSegment(segment.id, draft) : await api.retranslate(segment.id, kind);
      onChange(seg);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <aside className="editor" aria-label="段落编辑">
      <header>
        <strong>段落</strong>
        <span className={`badge ${segment.status}`}>{STATUS[segment.status]}</span>
        {segment.quality === "careful" && <span className="badge">精翻</span>}
        <span className="spacer" />
        <button type="button" className="link" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </header>
      <h3>原文</h3>
      <p className="source">{segment.source}</p>
      <h3>译文</h3>
      <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={8} />
      <p className="hint small">公式以 &lt;m1/&gt; 等占位符表示，修改时请保留。</p>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy !== null} onClick={() => void run("default")}>
          {busy === "default" ? "重翻中…" : "重翻"}
        </button>
        <button type="button" disabled={busy !== null} onClick={() => void run("careful")}>
          {busy === "careful" ? "精修中…" : "精修（开思考）"}
        </button>
        <span className="spacer" />
        <button
          type="button"
          className="primary"
          disabled={busy !== null || !draft.trim() || draft === segment.target}
          onClick={() => void run("save")}
        >
          {busy === "save" ? "保存中…" : "保存修改"}
        </button>
      </div>
    </aside>
  );
}
