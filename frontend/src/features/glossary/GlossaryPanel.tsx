/**
 * “我的术语库”：导入文件（规则识别，或用翻译服务的 AI 识别任意格式）→ 逐条确认 → 保存在浏览器里；
 * 也可以手动添加、搜索、修改、删除、导出 CSV。新建任务与单段重翻时自动带上。
 */
import { useEffect, useMemo, useState } from "react";

import {
  MAX_TERMS,
  findTerm,
  removeTerm,
  toCsv,
  upsertTerms,
  useTerms,
  type IncomingTerm,
  type TermEntry,
} from "@/entities/glossary/model";
import { api, type ParsedGlossary } from "@/shared/api/client";

interface Props {
  onClose: () => void;
}

interface Candidate extends IncomingTerm {
  keep: boolean;
}

export function GlossaryPanel({ onClose }: Props) {
  const terms = useTerms();
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<IncomingTerm>({ source: "", target: "", note: "" });
  const [editing, setEditing] = useState<string | null>(null);
  const [edit, setEdit] = useState<IncomingTerm>({ source: "", target: "", note: "" });
  const [file, setFile] = useState<File | null>(null);
  const [useAi, setUseAi] = useState(false);
  const [busy, setBusy] = useState(false);
  const [parsed, setParsed] = useState<Omit<ParsedGlossary, "entries"> | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q ? terms.filter((t) => [t.source, t.target, t.note].some((s) => s.toLowerCase().includes(q))) : terms;
    return [...list].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }, [terms, query]);

  const recognize = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const { entries, ...meta } = await api.parseGlossary(file, useAi);
      setParsed(meta);
      setCandidates(entries.map((e) => ({ ...e, keep: true })));
      if (entries.length === 0)
        setError(useAi ? "没有找到术语。" : "没认出术语。可以勾选“用 AI 识别”再试一次，或改成“英文,中文,说明”的 CSV。");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const saveCandidates = () => {
    const { added, updated } = upsertTerms(candidates.filter((c) => c.keep));
    setMessage(`已保存：新增 ${added} 条，更新 ${updated} 条。`);
    setCandidates([]);
    setParsed(null);
    setFile(null);
  };

  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([toCsv()], { type: "text/csv;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "我的术语库.csv" });
    a.click();
    URL.revokeObjectURL(url);
  };

  const setCandidate = (i: number, patch: Partial<Candidate>) =>
    setCandidates((list) => list.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const kept = candidates.filter((c) => c.keep).length;

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <div
        className="dialog glossary"
        role="dialog"
        aria-modal="true"
        aria-labelledby="glossary-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-head">
          <h2 id="glossary-title">我的术语库</h2>
          <span className="muted num">{terms.length} 条</span>
          <span className="spacer" />
          <button type="button" className="quiet" onClick={onClose}>
            关闭
          </button>
        </div>
        <p className="hint">
          只保存在这个浏览器里。新建任务和重翻段落时自动带上，优先于自动抽取的术语和公共词库；说明只给自己看，不发给翻译服务。
        </p>

        <section>
          <h3>从文件导入</h3>
          <div className="glossary-import">
            <input
              type="file"
              accept=".csv,.tsv,.txt,.md,.json,text/plain,text/csv,application/json"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setCandidates([]);
                setParsed(null);
              }}
            />
            <label className="choice">
              <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} />
              <span>
                用 AI 识别
                <small>任意格式都能读（如 Word 另存的文本、带说明的列表），会用你设置的翻译服务、花少量费用。</small>
              </span>
            </label>
            <button type="button" disabled={!file || busy} onClick={() => void recognize()}>
              {busy ? "识别中…" : "识别"}
            </button>
          </div>
          <p className="hint">
            表格文件（CSV、TSV、Markdown 表格）不用 AI 也能识别，列的顺序不限；识别结果确认后才会保存。
          </p>

          {parsed && candidates.length > 0 && (
            <div className="glossary-review">
              <p className="hint">
                {parsed.method === "ai" ? "AI 识别" : "按格式识别"}：文件 {parsed.lines} 行，认出 {candidates.length}{" "}
                条。请核对，取消勾选不要的，可以直接修改。
              </p>
              <div className="term-table" role="table" aria-label="待确认的术语">
                {candidates.map((c, i) => {
                  const old = findTerm(c.source, terms);
                  return (
                    <div className="term-row" role="row" key={i}>
                      <input
                        type="checkbox"
                        checked={c.keep}
                        onChange={(e) => setCandidate(i, { keep: e.target.checked })}
                        aria-label="保存这一条"
                      />
                      <input
                        value={c.source}
                        onChange={(e) => setCandidate(i, { source: e.target.value })}
                        aria-label="英文"
                      />
                      <input
                        value={c.target}
                        onChange={(e) => setCandidate(i, { target: e.target.value })}
                        aria-label="中文"
                      />
                      <input
                        value={c.note}
                        onChange={(e) => setCandidate(i, { note: e.target.value })}
                        placeholder="说明"
                        aria-label="说明"
                      />
                      {old && old.target !== c.target && (
                        <small className="term-conflict">已有“{old.target}”，保存后替换</small>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="actions">
                <button type="button" className="quiet" onClick={() => setCandidates([])}>
                  放弃
                </button>
                <button type="button" className="primary" disabled={kept === 0} onClick={saveCandidates}>
                  保存选中的 {kept} 条
                </button>
              </div>
            </div>
          )}
          {error && <p className="error">{error}</p>}
          {message && <p className="check-ok">{message}</p>}
        </section>

        <section>
          <h3>手动添加</h3>
          <form
            className="term-row term-add"
            onSubmit={(e) => {
              e.preventDefault();
              upsertTerms([draft]);
              setMessage(`已保存“${draft.source.trim()}”。`);
              setDraft({ source: "", target: "", note: "" });
            }}
          >
            <input
              value={draft.source}
              onChange={(e) => setDraft({ ...draft, source: e.target.value })}
              placeholder="英文术语"
              aria-label="英文术语"
            />
            <input
              value={draft.target}
              onChange={(e) => setDraft({ ...draft, target: e.target.value })}
              placeholder="译法"
              aria-label="译法"
            />
            <input
              value={draft.note}
              onChange={(e) => setDraft({ ...draft, note: e.target.value })}
              placeholder="说明（可选）"
              aria-label="说明"
            />
            <button type="submit" disabled={!draft.source.trim() || !draft.target.trim() || terms.length >= MAX_TERMS}>
              添加
            </button>
          </form>
        </section>

        <section>
          <div className="glossary-list-head">
            <h3>全部词条</h3>
            <span className="spacer" />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索英文、译法或说明"
              aria-label="搜索术语"
            />
            <button type="button" className="quiet" disabled={terms.length === 0} onClick={exportCsv}>
              导出 CSV
            </button>
          </div>
          {terms.length === 0 ? (
            <p className="muted">还没有词条。可以导入文件、手动添加，或在段落编辑里把纠正的术语加进来。</p>
          ) : (
            <div className="term-table" role="table" aria-label="术语库">
              {shown.map((t) =>
                editing === t.source ? (
                  <div className="term-row" role="row" key={t.source}>
                    <input
                      value={edit.source}
                      onChange={(e) => setEdit({ ...edit, source: e.target.value })}
                      aria-label="英文"
                    />
                    <input
                      value={edit.target}
                      onChange={(e) => setEdit({ ...edit, target: e.target.value })}
                      aria-label="中文"
                    />
                    <input
                      value={edit.note}
                      onChange={(e) => setEdit({ ...edit, note: e.target.value })}
                      aria-label="说明"
                    />
                    <button
                      type="button"
                      className="primary"
                      disabled={!edit.source.trim() || !edit.target.trim()}
                      onClick={() => {
                        if (edit.source.trim().toLowerCase() !== t.source.toLowerCase()) removeTerm(t.source);
                        upsertTerms([edit]);
                        setEditing(null);
                      }}
                    >
                      保存
                    </button>
                    <button type="button" className="quiet" onClick={() => setEditing(null)}>
                      取消
                    </button>
                  </div>
                ) : (
                  <TermLine
                    key={t.source}
                    term={t}
                    onEdit={() => {
                      setEditing(t.source);
                      setEdit({ source: t.source, target: t.target, note: t.note });
                    }}
                  />
                ),
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function TermLine({ term, onEdit }: { term: TermEntry; onEdit: () => void }) {
  return (
    <div className="term-row term-line" role="row">
      <span className="term-source">{term.source}</span>
      <span>{term.target}</span>
      <span className="muted">{term.note}</span>
      <button type="button" className="quiet" onClick={onEdit}>
        修改
      </button>
      <button
        type="button"
        className="quiet"
        onClick={() => window.confirm(`删除“${term.source}”？`) && removeTerm(term.source)}
      >
        删除
      </button>
    </div>
  );
}
