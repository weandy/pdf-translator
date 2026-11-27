import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { findTerm, termMap, upsertTerms } from "@/entities/glossary/model";
import { api, formulaUrl, type Quality, type SegmentData, type TranslationMode } from "@/shared/api/client";
import { hasOpenModal } from "@/shared/feedback/ConfirmDialog";
import { useToast } from "@/shared/feedback/Toast";
import { copyFormula, copyTextWithFormulas, interceptMathSelectionCopy } from "@/shared/math/clipboard";
import { InlineMathText, renderLatex } from "@/shared/math/inline";
import { checkPlaceholders, placeholdersOk } from "@/shared/text/placeholders";

import { InlineFormulaEditor, type InlineFormulaEditorHandle } from "./InlineFormulaEditor";

interface Props {
  segment: SegmentData;
  channelMode?: TranslationMode;
  onChange: (seg: SegmentData) => void;
  onClose: () => void;
  onDirtyChange?: (dirty: boolean) => void;
  onStep?: (delta: -1 | 1) => void;
  canStepPrev?: boolean;
  canStepNext?: boolean;
  returnToTerms?: boolean;
  onReturnToTerms?: () => void;
  /** 这一段的修改记录（由页面组合） */
  history?: ReactNode;
  /** 页面组合的问题段落导航，便于逐段处理。 */
  navigation?: ReactNode;
}

const INSTRUCTION_CHIPS = ["更通顺自然", "更严谨直译", "偏精炼"];

const STATUS: Record<SegmentData["status"], string> = {
  pending: "未翻译",
  done: "已翻译",
  failed: "未完成翻译",
  edited: "已手动修改",
};

function initialFormulaMap(formulas: SegmentData["formulas"]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(formulas)) out[k] = v.latex;
  return out;
}

/** 切换段落、译文或公式更新时由调用方用 key 重新挂载，草稿从 props 初始化。 */
export function SegmentEditor({
  segment,
  channelMode,
  onChange,
  onClose,
  onDirtyChange,
  onStep,
  canStepPrev = false,
  canStepNext = false,
  returnToTerms = false,
  onReturnToTerms,
  history,
  navigation,
}: Props) {
  const toast = useToast();
  const [draft, setDraft] = useState(segment.target ?? "");
  const [formulaDrafts, setFormulaDrafts] = useState<Record<string, string>>(() => initialFormulaMap(segment.formulas));
  const [activeFormula, setActiveFormula] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | Quality | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryFailed, setRetryFailed] = useState(false);
  const [instruction, setInstruction] = useState("");
  const mountedRef = useRef(false);
  const reqSeqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      reqSeqRef.current += 1;
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [segment.id]);

  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const markCopied = (key: string) => {
    setCopiedKey(key);
    window.setTimeout(() => setCopiedKey((current) => (current === key ? null : current)), 1600);
  };

  const formulaEditorRef = useRef<InlineFormulaEditorHandle>(null);
  const formulaKeys = useMemo(
    () => Object.keys(segment.formulas).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1))),
    [segment.formulas],
  );
  const hasFormulas = formulaKeys.length > 0;

  // 合并用户正在编辑的 LaTeX 草稿，实时驱动原文展示、译文内联公式与双格式复制
  const effectiveFormulas = useMemo(() => {
    const out: SegmentData["formulas"] = {};
    for (const key of formulaKeys) {
      const base = segment.formulas[key];
      if (!base) continue;
      out[key] = { ...base, latex: formulaDrafts[key] ?? base.latex };
    }
    return out;
  }, [formulaKeys, segment.formulas, formulaDrafts]);

  const changedFormulas = useMemo(() => {
    const out: Record<string, string> = {};
    for (const key of formulaKeys) {
      const orig = (segment.formulas[key]?.latex ?? "").trim();
      const next = (formulaDrafts[key] ?? "").trim();
      if (next !== orig) out[key] = next;
    }
    return out;
  }, [formulaKeys, segment.formulas, formulaDrafts]);

  const formulaChangedCount = Object.keys(changedFormulas).length;
  const invalidFormulas = useMemo(
    () =>
      formulaKeys.filter((key) => {
        const orig = (segment.formulas[key]?.latex ?? "").trim();
        const next = (formulaDrafts[key] ?? "").trim();
        if (next === orig) return false;
        return !next || renderLatex(next) === null;
      }),
    [formulaKeys, segment.formulas, formulaDrafts],
  );

  const check = checkPlaceholders(segment.source, draft);
  const formulasOk = placeholdersOk(check) && invalidFormulas.length === 0;

  const formulaName = (key: string) => `公式 ${key.slice(1)}`;
  const reinsertFormula = (key: string) => {
    if (formulaEditorRef.current) {
      formulaEditorRef.current.insertFormula(key);
    } else {
      setDraft((prev) => `${prev}<${key}/>`);
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !e.defaultPrevented && !hasOpenModal() && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 原文划词按需弹出单行「存入我的术语库」快捷条，平时零干扰
  const [quickTerm, setQuickTerm] = useState<{ source: string; target: string } | null>(null);
  const pickSourceSelection = (container: HTMLDivElement) => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
    if (!container.contains(sel.anchorNode) || !container.contains(sel.focusNode)) return;
    const picked = sel.toString().replace(/\s+/g, " ").trim();
    if (!picked || picked.length > 80 || picked.includes("<m")) return;
    const old = findTerm(picked);
    setQuickTerm({ source: picked, target: old?.target ?? "" });
  };

  const saveQuickTerm = () => {
    if (!quickTerm || !quickTerm.source.trim() || !quickTerm.target.trim()) return;
    upsertTerms([{ source: quickTerm.source.trim(), target: quickTerm.target.trim(), note: "" }]);
    toast.success(`已将“${quickTerm.source.trim()}”存入我的术语库`);
    setQuickTerm(null);
  };

  const copyBlock = async (kind: "source" | "target") => {
    const text = kind === "source" ? segment.source : draft;
    const formulaMap = kind === "source" ? segment.formulas : effectiveFormulas;
    try {
      await copyTextWithFormulas(text, formulaMap);
      toast.success(kind === "source" ? "已复制原文（含标准公式）" : "已复制译文（含标准公式）");
      markCopied(kind);
    } catch {
      const message = "复制失败，请检查浏览器的剪贴板权限";
      setError(message);
      toast.error(message);
    }
  };

  const copySingleFormula = async (key: string) => {
    const latex = (formulaDrafts[key] ?? segment.formulas[key]?.latex ?? "").trim();
    if (!latex) return;
    try {
      await copyFormula(latex, false);
      toast.success(`已复制 ${formulaName(key)}（支持 LaTeX 与 Word 公式）`);
      markCopied(`formula:${key}`);
    } catch {
      const message = "复制失败，请检查浏览器的剪贴板权限";
      setError(message);
      toast.error(message);
    }
  };

  const resetDrafts = () => {
    setDraft(segment.target ?? "");
    setFormulaDrafts(initialFormulaMap(segment.formulas));
    setActiveFormula(null);
    setError(null);
  };

  const run = async (kind: "save" | Quality) => {
    if (abortRef.current) return;
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++reqSeqRef.current;
    const current = () => mountedRef.current && seq === reqSeqRef.current;
    setBusy(kind);
    setError(null);
    setRetryFailed(false);
    try {
      if (kind === "save") {
        const seg =
          formulaChangedCount > 0
            ? await api.editSegment(segment.id, draft, controller.signal, changedFormulas)
            : await api.editSegment(segment.id, draft, controller.signal);
        if (!current()) return;
        onChange(seg);
        toast.success(formulaChangedCount > 0 ? "已保存译文与公式修改" : "已保存段落修改");
        return;
      }
      const seg = await api.retranslate(segment.id, kind, termMap(), {
        channelMode,
        instruction,
        signal: controller.signal,
      });
      if (!current()) return;
      onChange(seg);
      if (seg.applied === false) {
        setError(seg.failure_reason || seg.notice || "本次重翻未应用");
        setRetryFailed(true);
        toast.error(seg.notice || "重翻未通过校验");
        return;
      }
      setInstruction("");
      toast.success(kind === "careful" ? "精修完成" : "重翻完成");
    } catch (e) {
      if (!current()) return;
      const message = e instanceof Error ? e.message : String(e);
      setError(message);
      toast.error(message);
      setRetryFailed(kind !== "save");
    } finally {
      if (current()) {
        abortRef.current = null;
        setBusy(null);
      }
    }
  };

  const failure = error ?? (busy === null ? segment.failure_reason : "");
  const attempts = segment.translation_attempts ?? 0;
  const keptTranslation = Boolean(segment.target) && (segment.status === "done" || segment.status === "edited");
  const textDirty = draft !== (segment.target ?? "");
  const dirty = textDirty || formulaChangedCount > 0;
  const canSave = busy === null && Boolean(draft.trim()) && dirty && formulasOk;
  const showAttempts = busy === null && (attempts > 1 || (attempts > 0 && (segment.status === "failed" || Boolean(failure))));

  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  const saveShortcutRef = useRef<() => void>(() => {});
  useEffect(() => {
    saveShortcutRef.current = () => {
      if (canSave) void run("save");
    };
  });
  useEffect(() => {
    const onSaveKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing || hasOpenModal()) return;
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        saveShortcutRef.current();
      }
    };
    document.addEventListener("keydown", onSaveKey);
    return () => document.removeEventListener("keydown", onSaveKey);
  }, []);

  const activeLatex = activeFormula ? (formulaDrafts[activeFormula] ?? "") : "";
  const activeOrigLatex = activeFormula ? (segment.formulas[activeFormula]?.latex ?? "") : "";
  const activeFormulaHtml = activeFormula && activeLatex.trim() ? renderLatex(activeLatex.trim()) : null;
  const activeFormulaInvalid = Boolean(activeFormula && (!activeLatex.trim() || activeFormulaHtml === null));
  const activeFormulaDirty = Boolean(activeFormula && activeLatex.trim() !== activeOrigLatex.trim());

  return (
    <aside className="editor editor-segment" aria-label="段落编辑">
      <span className="editor-grip" aria-hidden="true" />
      {returnToTerms && onReturnToTerms && (
        <div className="editor-return-bar">
          <button type="button" className="quiet editor-return-btn" onClick={onReturnToTerms}>
            <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
              <polyline points="15 18 9 12 15 6" />
            </svg>
            <span>返回「本文术语」</span>
          </button>
        </div>
      )}
      <header className="editor-head">
        <strong>段落</strong>
        <span className={`tag ${segment.status}`}>{STATUS[segment.status]}</span>
        {segment.quality === "careful" && <span className="tag">精翻</span>}
        {dirty && <span className="unsaved-tag">未保存</span>}
        <span className="spacer" />
        {onStep && (
          <div className="editor-step-group" role="group" aria-label="切换相邻段落">
            <button
              type="button"
              className="quiet"
              disabled={busy !== null || !canStepPrev}
              onClick={() => onStep(-1)}
              aria-label="上一段"
              title="上一段"
            >
              ‹
            </button>
            <button
              type="button"
              className="quiet"
              disabled={busy !== null || !canStepNext}
              onClick={() => onStep(1)}
              aria-label="下一段"
              title="下一段"
            >
              ›
            </button>
          </div>
        )}
        <button type="button" className="quiet" onClick={onClose} aria-label="关闭段落编辑">
          关闭
        </button>
      </header>

      <div className="editor-body">
        {navigation}
        {busy !== null && busy !== "save" && (
          <p className="hint" role="status">
            正在翻译，失败时会自动重试，单段最多尝试 10 次，本次最多等待 45 秒，请勿重复操作。
          </p>
        )}
        {showAttempts && <p className="hint">最近一次翻译实际尝试 {attempts} 次（最多 10 次）。</p>}
        {failure && (
          <div className="error" role="alert">
            {keptTranslation && (segment.failure_reason || retryFailed) && <p>本次重翻未成功，已保留上次译文。</p>}
            <p>{failure}</p>
          </div>
        )}

        {/* ① 原文参考区：直接渲染数学公式，支持划词按需存入个人术语库与双格式复制 */}
        <section className="editor-section editor-source-section">
          <div className="editor-section-head">
            <h3>
              原文 <small className="muted">划词可存入术语库</small>
            </h3>
            <button type="button" className="copy-button" onClick={() => void copyBlock("source")}>
              {copiedKey === "source" ? "已复制" : "复制原文"}
            </button>
          </div>
          <div
            className="editor-source-rendered"
            aria-label="原文"
            onMouseUp={(e) => pickSourceSelection(e.currentTarget)}
            onCopy={interceptMathSelectionCopy}
          >
            <InlineMathText
              text={segment.source}
              formulas={segment.formulas}
              fallbackUrl={(k) => formulaUrl(segment.id, k)}
            />
          </div>
          {quickTerm && (
            <form
              className="term-inline-bar"
              aria-label="快捷存入个人术语库"
              onSubmit={(e) => {
                e.preventDefault();
                saveQuickTerm();
              }}
            >
              <span className="term-inline-src" title={quickTerm.source}>
                {quickTerm.source}
              </span>
              <span className="muted">→</span>
              <input
                value={quickTerm.target}
                onChange={(e) => setQuickTerm({ ...quickTerm, target: e.target.value })}
                placeholder="输入中文译法"
                aria-label="术语译法"
                autoFocus
              />
              <button type="submit" className="quiet" disabled={!quickTerm.target.trim()}>
                存术语
              </button>
              <button type="button" className="quiet" onClick={() => setQuickTerm(null)} aria-label="取消存术语">
                ×
              </button>
            </form>
          )}
        </section>

        {/* ② 译文主编辑区：含公式段落直接所见即所得展示，悬停看原始 LaTeX / 点击就地修改与复制 */}
        <section className="editor-section editor-target-section">
          <div className="editor-section-head">
            <h3>
              译文
              {hasFormulas && <small className="muted">悬停或点击公式可复制与修改</small>}
            </h3>
            <button
              type="button"
              className="copy-button"
              disabled={!draft.trim()}
              onClick={() => void copyBlock("target")}
            >
              {copiedKey === "target" ? "已复制" : "复制译文"}
            </button>
          </div>

          {hasFormulas ? (
            <InlineFormulaEditor
              ref={formulaEditorRef}
              value={draft}
              formulas={effectiveFormulas}
              originalFormulas={segment.formulas}
              activeFormula={activeFormula}
              onChange={setDraft}
              onSelectFormula={(key) => setActiveFormula((cur) => (cur === key ? null : key))}
              onCopySuccess={(key) => toast.success(`已复制 ${formulaName(key)}（支持 LaTeX 与 Word 公式）`)}
              onCopyError={(msg) => {
                setError(msg);
                toast.error(msg);
              }}
            />
          ) : (
            <textarea
              className="editor-target-input"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              aria-label="译文"
              placeholder="输入或修改中文译文…"
            />
          )}

          {/* 点击译文内某个公式后，紧贴下方就地展开精简 LaTeX 修改条 */}
          {activeFormula && (
            <div className="formula-inline-editor" role="region" aria-label={`编辑 ${formulaName(activeFormula)}`}>
              <div className="formula-inline-head">
                <strong>编辑 {formulaName(activeFormula)}</strong>
                <div className="formula-inline-actions">
                  <button
                    type="button"
                    className="quiet"
                    disabled={!activeLatex.trim()}
                    onClick={() => void copySingleFormula(activeFormula)}
                    title="复制为标准 LaTeX 与 Word 公式"
                  >
                    {copiedKey === `formula:${activeFormula}` ? "已复制" : "复制公式"}
                  </button>
                  {activeFormulaDirty && (
                    <button
                      type="button"
                      className="quiet"
                      onClick={() =>
                        setFormulaDrafts((prev) => ({
                          ...prev,
                          [activeFormula]: activeOrigLatex,
                        }))
                      }
                    >
                      还原
                    </button>
                  )}
                  <button
                    type="button"
                    className="quiet"
                    onClick={() => setActiveFormula(null)}
                    aria-label="收起公式编辑"
                  >
                    完成
                  </button>
                </div>
              </div>
              <textarea
                className="formula-latex-input"
                rows={2}
                aria-label={`公式 ${activeFormula} 的 LaTeX`}
                value={activeLatex}
                onChange={(e) =>
                  setFormulaDrafts((prev) => ({
                    ...prev,
                    [activeFormula]: e.target.value,
                  }))
                }
                placeholder="输入标准 LaTeX（无需包裹 $），例如 d_k"
              />
              {activeFormulaInvalid ? (
                <div className="formula-inline-preview">
                  <span className="formula-inline-error" role="alert">
                    LaTeX 语法有误，请检查花括号或命令后再保存
                  </span>
                </div>
              ) : (
                <p className="hint formula-inline-note">
                  修改后上方译文与复制实时生效；导出 PDF 仍沿用原论文高清公式原图。
                </p>
              )}
            </div>
          )}

          {!placeholdersOk(check) && (
            <div className="error formula-problems" role="alert">
              {check.missing.length > 0 && (
                <p>
                  译文中缺少 {check.missing.map(formulaName).join("、")}，保存前请补回：
                  {check.missing.map((k) => {
                    const html = renderLatex((formulaDrafts[k] ?? "").trim());
                    return (
                      <button key={k} type="button" className="formula-restore-btn" onClick={() => reinsertFormula(k)}>
                        补回 {html ? <span dangerouslySetInnerHTML={{ __html: html }} /> : formulaName(k)}
                      </button>
                    );
                  })}
                </p>
              )}
              {check.repeated.length > 0 && (
                <p>{check.repeated.map(formulaName).join("、")} 出现了不止一次，请删掉多余的。</p>
              )}
              {check.unknown.length > 0 && <p>{check.unknown.map((k) => `<${k}/>`).join("、")} 在原文里不存在，请删掉。</p>}
            </div>
          )}
        </section>

        {/* ③ AI 辅助重翻区：要求与重翻/精修按钮收口在一张卡内 */}
        <section className="editor-ai-card" aria-label="AI 重翻与精修">
          <div className="editor-section-head">
            <h3>AI 重翻与精修</h3>
            <span className="muted">可选填要求后直接重翻</span>
          </div>
          <input
            aria-label="修改要求"
            placeholder="例如：更符合中文学术习惯 / 保留英文方法名"
            maxLength={200}
            disabled={busy !== null}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
          />
          <div className="editor-ai-footer">
            <div className="chips">
              {INSTRUCTION_CHIPS.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  className="quiet"
                  disabled={
                    busy !== null ||
                    instruction.includes(chip) ||
                    instruction.trim().length + chip.length + (instruction.trim() ? 1 : 0) > 200
                  }
                  onClick={() => setInstruction((value) => [value.trim(), chip].filter(Boolean).join("、"))}
                >
                  {chip}
                </button>
              ))}
            </div>
            <div className="editor-ai-triggers">
              <button type="button" disabled={busy !== null} onClick={() => void run("default")}>
                {busy === "default" ? "重翻中…" : "重翻"}
              </button>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void run("careful")}
                title="重新翻译这一段，思考强度依服务商和模型支持而定，费用按实际用量计"
              >
                {busy === "careful" ? "精修中…" : "精修"}
              </button>
            </div>
          </div>
        </section>

        {/* ④ 本段修改记录沉底折叠，不打断校对主线 */}
        {history && (
          <details className="segment-history-fold">
            <summary>这一段的修改记录</summary>
            <div className="segment-history-body">{history}</div>
          </details>
        )}
      </div>

      {/* ⑤ 纯粹的手动保存底栏 */}
      <div className="actions editor-actions">
        {dirty ? (
          <button type="button" className="quiet" disabled={busy !== null} onClick={resetDrafts}>
            还原未保存
          </button>
        ) : (
          <span className="editor-shortcut-hint">Ctrl+S 保存</span>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="primary"
          disabled={!canSave}
          title={
            !formulasOk
              ? invalidFormulas.length > 0
                ? "有公式 LaTeX 语法有误，请先修正"
                : "公式不完整，请先按提示补回"
              : "保存修改 (Ctrl+S / ⌘S)"
          }
          onClick={() => void run("save")}
        >
          {busy === "save" ? "保存中…" : "保存修改"}
        </button>
      </div>
    </aside>
  );
}
