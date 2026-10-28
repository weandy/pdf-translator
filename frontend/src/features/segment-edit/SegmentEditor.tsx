import { useMemo, useState, type ReactNode } from "react";

import { findTerm, termMap, upsertTerms, useTerms } from "@/entities/glossary/model";
import { api, type Quality, type SegmentData } from "@/shared/api/client";

interface Props {
  segment: SegmentData;
  onChange: (seg: SegmentData) => void;
  onClose: () => void;
  /** 这一段的修改记录（由页面组合） */
  history?: ReactNode;
}

const STATUS: Record<SegmentData["status"], string> = {
  pending: "未翻译",
  done: "已翻译",
  failed: "翻译失败",
  edited: "已手动修改",
};

/** 切换段落或译文更新时由调用方用 key 重新挂载，草稿从 props 初始化。 */
export function SegmentEditor({ segment, onChange, onClose, history }: Props) {
  const [draft, setDraft] = useState(segment.target ?? "");
  const [busy, setBusy] = useState<"save" | Quality | null>(null);
  const [error, setError] = useState<string | null>(null);
  const terms = useTerms();
  const [term, setTerm] = useState({ source: "", target: "", note: "" });
  const [termSaved, setTermSaved] = useState<string | null>(null);
  // 这一段原文里出现的“我的术语库”词条（整词、不分大小写）
  const hits = useMemo(() => {
    const text = segment.source.toLowerCase();
    return terms.filter((t) => {
      const i = text.indexOf(t.source.toLowerCase());
      const edge = (c: string | undefined) => !c || !/[a-z0-9]/.test(c);
      return i >= 0 && edge(text[i - 1]) && edge(text[i + t.source.length]);
    });
  }, [terms, segment.source]);

  // 在原文框里选中一段文字，就填进“加入术语库”的英文栏；已在术语库里的带出原来的译法与说明
  const pickSelection = (el: HTMLTextAreaElement) => {
    const picked = el.value.slice(el.selectionStart, el.selectionEnd).trim();
    if (!picked || picked.length > 120 || picked.includes("<m")) return;
    const old = findTerm(picked);
    setTerm({ source: picked, target: old?.target ?? "", note: old?.note ?? "" });
    setTermSaved(null);
  };
  const saveTerm = () => {
    upsertTerms([term]);
    setTermSaved(`已把“${term.source.trim()}”存进术语库。点“重翻”可以按新译法重新翻译这一段。`);
    setTerm({ source: "", target: "", note: "" });
  };

  const run = async (kind: "save" | Quality) => {
    setBusy(kind);
    setError(null);
    try {
      const seg =
        kind === "save" ? await api.editSegment(segment.id, draft) : await api.retranslate(segment.id, kind, termMap());
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
        <span className={`tag ${segment.status}`}>{STATUS[segment.status]}</span>
        {segment.quality === "careful" && <span className="tag">精翻</span>}
        <span className="spacer" />
        <button type="button" className="quiet" onClick={onClose} aria-label="关闭段落编辑">
          关闭
        </button>
      </header>
      {/* 原文与译文用同样大小的框，原文只读 */}
      <div className="panes">
        <h3>
          原文 <small className="muted">只读</small>
        </h3>
        <textarea
          className="source"
          value={segment.source}
          readOnly
          aria-label="原文"
          onSelect={(e) => pickSelection(e.currentTarget)}
          onMouseUp={(e) => pickSelection(e.currentTarget)}
          onKeyUp={(e) => pickSelection(e.currentTarget)}
        />
        <h3>译文</h3>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="译文" />
      </div>
      <p className="hint">公式显示为 &lt;m1/&gt; 这样的占位符，修改时请保留。</p>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" disabled={busy !== null} onClick={() => void run("default")}>
          {busy === "default" ? "重翻中…" : "重翻"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void run("careful")}
          title="开启低强度思考重新翻译这一段，费用约为普通的 4 倍"
        >
          {busy === "careful" ? "精修中…" : "精修"}
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
      <section className="segment-terms">
        <h3>术语</h3>
        {hits.length > 0 && (
          <p className="hint">
            本段用到术语库里的：
            {hits.map((t) => (
              <span key={t.source} className="term-chip" title={t.note || undefined}>
                {t.source} → {t.target}
              </span>
            ))}
          </p>
        )}
        <p className="hint">在原文框里选中一个术语，填上正确译法，存进“我的术语库”，以后的任务都会用上。</p>
        <form
          className="term-quick"
          onSubmit={(e) => {
            e.preventDefault();
            saveTerm();
          }}
        >
          <input
            value={term.source}
            onChange={(e) => setTerm({ ...term, source: e.target.value })}
            placeholder="英文术语"
            aria-label="英文术语"
          />
          <input
            value={term.target}
            onChange={(e) => setTerm({ ...term, target: e.target.value })}
            placeholder="正确译法"
            aria-label="正确译法"
          />
          <input
            value={term.note}
            onChange={(e) => setTerm({ ...term, note: e.target.value })}
            placeholder="说明（可选，如为什么这样译）"
            aria-label="说明"
          />
          <button type="submit" disabled={!term.source.trim() || !term.target.trim()}>
            加入术语库
          </button>
        </form>
        {termSaved && <p className="check-ok">{termSaved}</p>}
      </section>
      {history && (
        <section className="segment-history">
          <h3>这一段的修改记录</h3>
          {history}
        </section>
      )}
    </aside>
  );
}
