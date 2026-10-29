/**
 * 我的术语库（独立页面）：搜索、排序、行内修改、批量删除（可撤销）、导入、导出。
 * 只保存在这个浏览器里；新建任务和重翻段落时自动带上，优先于自动抽取的术语和公共词库。
 */
import { useEffect, useMemo, useRef, useState } from "react";

import {
  MAX_TERMS,
  removeTerm,
  removeTerms,
  restoreTerms,
  toCsv,
  toJson,
  toTsv,
  upsertTerms,
  useTerms,
  type IncomingTerm,
  type TermEntry,
} from "@/entities/glossary/model";
import { GlossaryImport } from "@/features/glossary/GlossaryImport";

const PAGE = 100;
const EMPTY: IncomingTerm = { source: "", target: "", note: "" };
const key = (source: string) => source.trim().toLowerCase();

function download(text: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  URL.revokeObjectURL(url);
}

export function GlossaryPage() {
  const terms = useTerms();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"recent" | "alpha">("recent");
  const [limit, setLimit] = useState(PAGE);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<IncomingTerm>(EMPTY);
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [notice, setNotice] = useState<{ text: string; undo?: TermEntry[] } | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => {
    document.title = "我的术语库";
  }, []);

  const notify = (text: string, undo?: TermEntry[]) => {
    clearTimeout(noticeTimer.current);
    setNotice({ text, undo });
    noticeTimer.current = setTimeout(() => setNotice(null), 8000);
  };

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q ? terms.filter((t) => [t.source, t.target, t.note].some((s) => s.toLowerCase().includes(q))) : terms;
    return [...list].sort((a, b) =>
      sort === "alpha"
        ? a.source.localeCompare(b.source, "en", { sensitivity: "base" })
        : b.updatedAt.localeCompare(a.updatedAt),
    );
  }, [terms, query, sort]);
  const visible = shown.slice(0, limit);
  const allShownSelected = shown.length > 0 && shown.every((t) => selected.has(key(t.source)));

  const startEdit = (t: TermEntry | null) => {
    setAdding(t === null);
    setEditing(t ? key(t.source) : null);
    setDraft(t ? { source: t.source, target: t.target, note: t.note } : EMPTY);
  };
  const cancelEdit = () => {
    setAdding(false);
    setEditing(null);
  };
  const saveEdit = (original: TermEntry | null) => {
    if (!draft.source.trim() || !draft.target.trim()) return;
    if (original && key(original.source) !== key(draft.source)) removeTerm(original.source); // 改了英文：换一条
    upsertTerms([draft]);
    notify(original ? `已保存“${draft.source.trim()}”。` : `已添加“${draft.source.trim()}”。`);
    cancelEdit();
  };

  const toggle = (t: TermEntry, on: boolean) =>
    setSelected((s) => {
      const next = new Set(s);
      if (on) next.add(key(t.source));
      else next.delete(key(t.source));
      return next;
    });
  const deleteSelected = () => {
    const removed = removeTerms([...selected]);
    setSelected(new Set());
    notify(`已删除 ${removed.length} 条。`, removed);
  };

  const editor = (original: TermEntry | null) => (
    <form
      className="gl-row gl-edit"
      onSubmit={(e) => {
        e.preventDefault();
        saveEdit(original);
      }}
      onKeyDown={(e) => e.key === "Escape" && cancelEdit()}
    >
      <span />
      <input
        autoFocus
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
        placeholder="说明（可选，只给自己看）"
        aria-label="说明"
      />
      <span className="gl-row-actions">
        <button type="submit" className="primary" disabled={!draft.source.trim() || !draft.target.trim()}>
          保存
        </button>
        <button type="button" className="quiet" onClick={cancelEdit}>
          取消
        </button>
      </span>
    </form>
  );

  return (
    <main className="gl-page">
      <header className="gl-head">
        <div>
          <h1>我的术语库</h1>
          <p className="hint">
            只保存在这个浏览器里。新建任务和重翻段落时自动带上，优先于自动抽取的术语和公共词库；说明只给自己看，不发给翻译服务。
          </p>
        </div>
        <span className="gl-count num">
          {terms.length} / {MAX_TERMS} 条
        </span>
      </header>

      <div className="gl-toolbar">
        <input
          type="search"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setLimit(PAGE);
          }}
          placeholder="搜索英文、译法或说明"
          aria-label="搜索术语"
        />
        <select value={sort} onChange={(e) => setSort(e.target.value as "recent" | "alpha")} aria-label="排序">
          <option value="recent">最近修改在前</option>
          <option value="alpha">按英文 A–Z</option>
        </select>
        <span className="spacer" />
        <button type="button" disabled={terms.length >= MAX_TERMS} onClick={() => startEdit(null)}>
          添加
        </button>
        <button type="button" className={importing ? "on" : ""} onClick={() => setImporting((v) => !v)}>
          导入
        </button>
        <div className="gl-export">
          <button
            type="button"
            disabled={terms.length === 0}
            aria-expanded={exportOpen}
            onClick={() => setExportOpen((v) => !v)}
          >
            导出
          </button>
          {exportOpen && (
            <div className="gl-menu" role="menu" onClick={() => setExportOpen(false)}>
              <button
                type="button"
                role="menuitem"
                onClick={() => download(toCsv(), "我的术语库.csv", "text/csv;charset=utf-8")}
              >
                CSV<small>Excel 可直接打开</small>
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => download(toTsv(), "我的术语库.tsv", "text/tab-separated-values;charset=utf-8")}
              >
                TSV<small>制表符分隔</small>
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => download(toJson(), "我的术语库.json", "application/json")}
              >
                JSON<small>备份或给程序用</small>
              </button>
            </div>
          )}
        </div>
      </div>

      {importing && (
        <GlossaryImport
          onDone={(text) => {
            setImporting(false);
            notify(text);
          }}
          onCancel={() => setImporting(false)}
        />
      )}

      {notice && (
        <p className="gl-notice" role="status">
          {notice.text}
          {notice.undo && notice.undo.length > 0 && (
            <button
              type="button"
              className="link"
              onClick={() => {
                restoreTerms(notice.undo ?? []);
                notify(`已恢复 ${notice.undo?.length ?? 0} 条。`);
              }}
            >
              撤销
            </button>
          )}
        </p>
      )}

      {selected.size > 0 && (
        <div className="gl-selection">
          已选 {selected.size} 条
          <button type="button" className="danger" onClick={deleteSelected}>
            删除
          </button>
          <button type="button" className="quiet" onClick={() => setSelected(new Set())}>
            取消选择
          </button>
        </div>
      )}

      <div className="gl-table" role="table" aria-label="术语库">
        <div className="gl-row gl-header" role="row">
          <input
            type="checkbox"
            checked={allShownSelected}
            disabled={shown.length === 0}
            onChange={(e) => setSelected(e.target.checked ? new Set(shown.map((t) => key(t.source))) : new Set())}
            aria-label={query ? "选中搜索到的全部" : "全选"}
          />
          <span>英文</span>
          <span>译法</span>
          <span>说明</span>
          <span className="muted num">{query ? `找到 ${shown.length} 条` : ""}</span>
        </div>
        {adding && editor(null)}
        {terms.length === 0 && !adding ? (
          <p className="gl-empty">
            还没有词条。点“添加”手动输入，点“导入”从文件或表格里批量导入；也可以在阅读器的段落编辑里把纠正过的术语加进来。
          </p>
        ) : shown.length === 0 ? (
          <p className="gl-empty">没有找到“{query}”。</p>
        ) : (
          visible.map((t) =>
            editing === key(t.source) ? (
              <div key={t.source}>{editor(t)}</div>
            ) : (
              <div
                className={`gl-row gl-line${selected.has(key(t.source)) ? " picked" : ""}`}
                role="row"
                key={t.source}
              >
                <input
                  type="checkbox"
                  checked={selected.has(key(t.source))}
                  onChange={(e) => toggle(t, e.target.checked)}
                  aria-label={`选中 ${t.source}`}
                />
                <button type="button" className="gl-cell gl-source" onClick={() => startEdit(t)} title="点击修改">
                  {t.source}
                </button>
                <button type="button" className="gl-cell" onClick={() => startEdit(t)} title="点击修改">
                  {t.target}
                </button>
                <button type="button" className="gl-cell muted" onClick={() => startEdit(t)} title="点击修改">
                  {t.note}
                </button>
                <span />
              </div>
            ),
          )
        )}
      </div>
      {shown.length > limit && (
        <button type="button" className="gl-more" onClick={() => setLimit((n) => n + PAGE)}>
          再显示 {Math.min(PAGE, shown.length - limit)} 条（共 {shown.length} 条）
        </button>
      )}
    </main>
  );
}
