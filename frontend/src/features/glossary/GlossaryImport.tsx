/**
 * 导入术语：选文件、拖入文件或直接粘贴文字 → 规则识别（或用 AI 识别）→ 分成新词条、译法不同、完全相同三组确认后保存。
 */
import { useState, type DragEvent } from "react";

import { planImport, upsertTerms, useTerms, type ImportPlan, type IncomingTerm } from "@/entities/glossary/model";
import { api, type ParsedGlossary } from "@/shared/api/client";

interface Props {
  onDone: (message: string) => void;
  onCancel: () => void;
}

interface Review {
  meta: Omit<ParsedGlossary, "entries">;
  plan: ImportPlan;
  keepFresh: boolean[]; // 新词条：是否保存
  fresh: IncomingTerm[]; // 新词条（可直接修改）
  useIncoming: boolean[]; // 译法不同：true 用导入的，false 保留原来的
}

export function GlossaryImport({ onDone, onCancel }: Props) {
  const terms = useTerms();
  const [source, setSource] = useState<"file" | "paste">("file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [useAi, setUseAi] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [dragging, setDragging] = useState(false);

  const input =
    source === "file" ? file : text.trim() ? new File([text], "粘贴的文字.txt", { type: "text/plain" }) : null;

  const recognize = async () => {
    if (!input) return;
    setBusy(true);
    setError(null);
    setReview(null);
    try {
      const { entries, ...meta } = await api.parseGlossary(input, useAi);
      if (entries.length === 0) {
        setError(useAi ? "没有找到术语。" : "没认出术语。可以勾选“用 AI 识别”再试一次，或改成“英文,中文,说明”的格式。");
        return;
      }
      const plan = planImport(entries, terms);
      setReview({
        meta,
        plan,
        fresh: plan.fresh,
        keepFresh: plan.fresh.map(() => true),
        useIncoming: plan.changed.map(() => true),
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) {
      setSource("file");
      setFile(f);
      setReview(null);
    }
  };

  const save = () => {
    if (!review) return;
    const items = [
      ...review.fresh.filter((_, i) => review.keepFresh[i]),
      ...review.plan.changed.filter((_, i) => review.useIncoming[i]).map((c) => c.incoming),
    ];
    const { added, updated } = upsertTerms(items);
    onDone(
      `导入完成：新增 ${added} 条，更新 ${updated} 条${review.plan.same ? `，${review.plan.same} 条与已有的相同已跳过` : ""}。`,
    );
  };

  const setFresh = (i: number, patch: Partial<IncomingTerm>) =>
    review && setReview({ ...review, fresh: review.fresh.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
  const toggle = (key: "keepFresh" | "useIncoming", i: number, value: boolean) =>
    review && setReview({ ...review, [key]: review[key].map((v, j) => (j === i ? value : v)) });
  const setAll = (key: "keepFresh" | "useIncoming", value: boolean) =>
    review && setReview({ ...review, [key]: review[key].map(() => value) });
  const count = review ? review.keepFresh.filter(Boolean).length + review.useIncoming.filter(Boolean).length : 0;

  return (
    <section className="gl-import" aria-label="导入术语">
      {!review ? (
        <>
          <div className="segmented" role="group" aria-label="导入方式">
            <button type="button" className={source === "file" ? "on" : ""} onClick={() => setSource("file")}>
              选文件
            </button>
            <button type="button" className={source === "paste" ? "on" : ""} onClick={() => setSource("paste")}>
              粘贴文字
            </button>
          </div>
          {source === "file" ? (
            <label
              className={`gl-drop${dragging ? " over" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
            >
              <input
                type="file"
                accept=".csv,.tsv,.txt,.md,.json,text/plain,text/csv,application/json"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setReview(null);
                }}
              />
              <span>{file ? file.name : "选择或拖入文件：CSV、TSV、Markdown 表格、JSON、纯文本"}</span>
            </label>
          ) : (
            <textarea
              className="gl-paste"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={6}
              placeholder={
                "每行一条，如：\nattention mechanism, 注意力机制\nbackbone\t骨干网络\t说明可以放第三列\n也可以从表格软件里直接复制两三列粘贴进来"
              }
              aria-label="粘贴术语"
            />
          )}
          <label className="choice">
            <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} />
            <span>
              用 AI 识别
              <small>格式不规整时用（如带说明的列表、Word 另存的文本），会用你设置的翻译服务，花少量费用。</small>
            </span>
          </label>
          {error && <p className="error">{error}</p>}
          <div className="actions">
            <button type="button" className="quiet" onClick={onCancel}>
              取消
            </button>
            <button type="button" className="primary" disabled={!input || busy} onClick={() => void recognize()}>
              {busy ? "识别中…" : "识别"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="hint">
            {review.meta.method === "ai" ? "AI 识别" : "按格式识别"}：{review.meta.lines} 行，认出{" "}
            {review.plan.fresh.length + review.plan.changed.length + review.plan.same} 条——新词条{" "}
            {review.plan.fresh.length}，译法不同 {review.plan.changed.length}，与已有的相同 {review.plan.same}（跳过）。
          </p>

          {review.fresh.length > 0 && (
            <div className="gl-group">
              <div className="gl-group-head">
                <h3>新词条</h3>
                <span className="spacer" />
                <button type="button" className="link" onClick={() => setAll("keepFresh", true)}>
                  全选
                </button>
                <button type="button" className="link" onClick={() => setAll("keepFresh", false)}>
                  全不选
                </button>
              </div>
              <div className="gl-rows">
                {review.fresh.map((t, i) => (
                  <div className="gl-row gl-edit" key={i}>
                    <input
                      type="checkbox"
                      checked={review.keepFresh[i]}
                      onChange={(e) => toggle("keepFresh", i, e.target.checked)}
                      aria-label="保存这一条"
                    />
                    <input
                      value={t.source}
                      onChange={(e) => setFresh(i, { source: e.target.value })}
                      aria-label="英文"
                    />
                    <input
                      value={t.target}
                      onChange={(e) => setFresh(i, { target: e.target.value })}
                      aria-label="译法"
                    />
                    <input
                      value={t.note}
                      onChange={(e) => setFresh(i, { note: e.target.value })}
                      placeholder="说明"
                      aria-label="说明"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {review.plan.changed.length > 0 && (
            <div className="gl-group">
              <div className="gl-group-head">
                <h3>和已有的不同</h3>
                <span className="spacer" />
                <button type="button" className="link" onClick={() => setAll("useIncoming", true)}>
                  全部用导入的
                </button>
                <button type="button" className="link" onClick={() => setAll("useIncoming", false)}>
                  全部保留原来的
                </button>
              </div>
              <div className="gl-rows">
                {review.plan.changed.map(({ incoming, old }, i) => (
                  <div className="gl-row gl-conflict" key={i} role="radiogroup" aria-label={incoming.source}>
                    <span className="gl-source">{old.source}</span>
                    <label className={review.useIncoming[i] ? "" : "on"}>
                      <input
                        type="radio"
                        checked={!review.useIncoming[i]}
                        onChange={() => toggle("useIncoming", i, false)}
                      />
                      原来：{old.target}
                      {old.note && <small>{old.note}</small>}
                    </label>
                    <label className={review.useIncoming[i] ? "on" : ""}>
                      <input
                        type="radio"
                        checked={review.useIncoming[i]}
                        onChange={() => toggle("useIncoming", i, true)}
                      />
                      导入：{incoming.target}
                      {incoming.note && <small>{incoming.note}</small>}
                    </label>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="actions">
            <button type="button" className="quiet" onClick={() => setReview(null)}>
              重新选择
            </button>
            <button type="button" className="primary" disabled={count === 0} onClick={save}>
              保存 {count} 条
            </button>
          </div>
        </>
      )}
    </section>
  );
}
