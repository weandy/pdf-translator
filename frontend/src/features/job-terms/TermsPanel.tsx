/**
 * 本文术语面板：这篇论文用到哪些术语、各出现在几段、哪几段的译文可能没用上推荐译法；
 * 点一个术语逐段查看；改译法后保存到我的术语库，并可分批重翻用到它的段落（手动改过的段落不动）。
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { termMap, upsertTerms, useTerms } from "@/entities/glossary/model";
import { api, type DocumentData, type SegmentData } from "@/shared/api/client";

import { ORIGIN_LABELS, termRows, type TermRow } from "./terms";

const CHUNK = 20; // 与后端一次重翻的上限一致

interface Props {
  jobId: string;
  doc: DocumentData;
  onChange: (seg: SegmentData) => void;
  onSpot: (segmentId: string) => void; // 滚到这一段并短暂高亮
  onMark: (segmentIds: string[]) => void; // 标出当前术语出现的所有段落
  onClose: () => void;
}

export function TermsPanel({ jobId, doc, onChange, onSpot, onMark, onClose }: Props) {
  const { data: uses, error } = useQuery({ queryKey: ["terms", jobId], queryFn: () => api.jobTerms(jobId) });
  const mine = useTerms();
  const rows = useMemo(() => termRows(uses ?? [], mine, doc.segments), [uses, mine, doc.segments]);
  const [query, setQuery] = useState("");
  const [onlyMismatch, setOnlyMismatch] = useState(false);
  // 公共词库按单词匹配，常见词（如 instance、cost）也会命中，默认只看本文抽取的和我的术语
  const [withLibrary, setWithLibrary] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const q = query.trim().toLowerCase();
  const libraryCount = rows.filter((r) => r.origin === "library").length;
  const pool = withLibrary ? rows : rows.filter((r) => r.origin !== "library");
  const shown = pool.filter(
    (r) =>
      (!onlyMismatch || r.mismatchedIds.length > 0) &&
      (!q || r.source.toLowerCase().includes(q) || r.target.toLowerCase().includes(q)),
  );
  const mismatchTerms = pool.filter((r) => r.mismatchedIds.length > 0).length;

  const toggle = (row: TermRow) => {
    const next = open === row.source ? null : row.source;
    setOpen(next);
    onMark(next ? row.segmentIds : []);
    if (next && row.segmentIds[0]) onSpot(row.mismatchedIds[0] ?? row.segmentIds[0]);
  };

  return (
    <aside className="editor terms-panel" aria-label="本文术语">
      <header>
        <strong>本文术语</strong>
        {uses && <span className="muted num">{pool.length} 个</span>}
        <span className="spacer" />
        <button
          type="button"
          className="quiet"
          onClick={() => {
            onMark([]);
            onClose();
          }}
        >
          关闭
        </button>
      </header>
      <p className="hint">翻译时用到的术语。红字是译文里可能没用上推荐译法的段落；改了译法可以一键重翻用到它的段落。</p>
      <div className="terms-filter">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索术语"
          aria-label="搜索术语"
        />
        <label className="choice-inline">
          <input type="checkbox" checked={onlyMismatch} onChange={(e) => setOnlyMismatch(e.target.checked)} />
          只看可能不一致（{mismatchTerms}）
        </label>
        {libraryCount > 0 && (
          <label className="choice-inline">
            <input type="checkbox" checked={withLibrary} onChange={(e) => setWithLibrary(e.target.checked)} />
            包含公共词库（{libraryCount}）
          </label>
        )}
      </div>
      {error && <p className="error">{String(error)}</p>}
      {!uses && !error && <p className="muted">正在查找…</p>}
      {uses && pool.length === 0 && (
        <p className="muted">
          {rows.length === 0 ? "这篇没有用到术语库里的术语。" : "本文没有抽取到术语，勾选“包含公共词库”查看词库匹配。"}
        </p>
      )}
      <ul className="terms-list">
        {shown.map((row) => (
          <li key={row.source} className={open === row.source ? "open" : ""}>
            <button type="button" className="term-item" aria-expanded={open === row.source} onClick={() => toggle(row)}>
              <span className="term-name">
                <span className="gl-source">{row.source}</span> <span>{row.target}</span>
              </span>
              <small>
                {ORIGIN_LABELS[row.origin]} · {row.segmentIds.length} 段
                {row.mismatchedIds.length > 0 && (
                  <span className="error"> · {row.mismatchedIds.length} 段可能不一致</span>
                )}
              </small>
            </button>
            {open === row.source && (
              <TermDetail row={row} jobId={jobId} doc={doc} onChange={onChange} onSpot={onSpot} />
            )}
          </li>
        ))}
      </ul>
    </aside>
  );
}

function TermDetail({
  row,
  jobId,
  doc,
  onChange,
  onSpot,
}: {
  row: TermRow;
  jobId: string;
  doc: DocumentData;
  onChange: (seg: SegmentData) => void;
  onSpot: (segmentId: string) => void;
}) {
  const [at, setAt] = useState(0);
  const [target, setTarget] = useState(row.target);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const ids = row.segmentIds;
  const go = (i: number) => {
    const n = (i + ids.length) % ids.length;
    setAt(n);
    const id = ids[n];
    if (id) onSpot(id);
  };
  const edited = new Set(doc.segments.filter((s) => s.status === "edited").map((s) => s.id));
  const redoAll = ids.filter((id) => !edited.has(id));
  const redoMismatch = row.mismatchedIds.filter((id) => !edited.has(id));
  const skipped = ids.length - redoAll.length;

  const save = () => {
    upsertTerms([{ source: row.source, target, note: "" }]);
    setMessage(`已保存到我的术语库：${row.source} = ${target.trim()}`);
  };

  const retranslate = async (list: string[]) => {
    setError(null);
    setMessage(null);
    if (target.trim() && target.trim() !== row.target) upsertTerms([{ source: row.source, target, note: "" }]); // 先保存改好的译法
    const terms = termMap();
    try {
      for (let i = 0; i < list.length; i += CHUNK) {
        setBusy(`正在重翻 ${i}/${list.length} 段…`);
        for (const seg of await api.retranslateMany(jobId, list.slice(i, i + CHUNK), terms)) onChange(seg);
      }
      setMessage(`已重翻 ${list.length} 段，可以在修改记录里逐段撤回。`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const dirty = target.trim() !== row.target || row.origin !== "user";
  return (
    <div className="term-detail">
      <div className="term-nav">
        <button type="button" className="quiet" onClick={() => go(at - 1)} aria-label="上一段">
          ‹
        </button>
        <span className="num">
          第 {at + 1} / {ids.length} 段
        </span>
        <button type="button" className="quiet" onClick={() => go(at + 1)} aria-label="下一段">
          ›
        </button>
        {row.changed && <small className="muted">建任务后改过译法</small>}
      </div>
      <form
        className="term-fix"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <input value={target} onChange={(e) => setTarget(e.target.value)} aria-label="译法" />
        <button type="submit" disabled={!target.trim() || !dirty}>
          {row.origin === "user" ? "保存译法" : "加入我的术语库"}
        </button>
      </form>
      {(redoMismatch.length > 0 || redoAll.length > 0) && (
        <div className="term-redo">
          {redoMismatch.length > 0 && redoMismatch.length < redoAll.length && (
            <button type="button" disabled={busy !== null} onClick={() => void retranslate(redoMismatch)}>
              重翻不一致的 {redoMismatch.length} 段
            </button>
          )}
          <button
            type="button"
            disabled={busy !== null || redoAll.length === 0}
            onClick={() => void retranslate(redoAll)}
          >
            重翻全部 {redoAll.length} 段
          </button>
        </div>
      )}
      <p className="hint">重翻按我的术语库里最新的译法{skipped > 0 ? `；${skipped} 段手动改过，不会重翻` : ""}。</p>
      {busy && <p className="muted">{busy}</p>}
      {message && <p className="check-ok">{message}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
