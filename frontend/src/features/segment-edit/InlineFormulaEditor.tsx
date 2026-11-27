import { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, forwardRef } from "react";

import type { SegmentData } from "@/shared/api/client";
import { copyFormula, interceptMathSelectionCopy } from "@/shared/math/clipboard";
import { renderLatex } from "@/shared/math/inline";
import { tokenize } from "@/shared/text/split";

export interface InlineFormulaEditorHandle {
  insertFormula: (key: string) => void;
  focus: () => void;
}

interface Props {
  value: string;
  formulas: SegmentData["formulas"];
  originalFormulas: SegmentData["formulas"];
  activeFormula: string | null;
  onChange: (next: string) => void;
  onSelectFormula: (key: string) => void;
  onCopySuccess?: (key: string) => void;
  onCopyError?: (message: string) => void;
}

interface HoverTip {
  key: string;
  latex: string;
  left: number;
  top: number;
}

/** 将 DOM 内的文字与原子公式节点序列化回带 <mN/> 占位符的译文字符串。 */
export function serializeEditorDom(root: HTMLElement): string {
  const walk = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) {
      return (node.nodeValue ?? "").replace(/\u200B/g, "");
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    const el = node as HTMLElement;
    const formulaKey = el.dataset.formulaKey;
    if (formulaKey) return `<${formulaKey}/>`;
    if (el.tagName === "BR") return "\n";

    let inner = "";
    for (const child of Array.from(el.childNodes)) {
      inner += walk(child);
    }
    if ((el.tagName === "DIV" || el.tagName === "P") && el.previousSibling) {
      return `\n${inner}`;
    }
    return inner;
  };

  let out = "";
  for (const child of Array.from(root.childNodes)) {
    out += walk(child);
  }
  // 去掉浏览器在空行尾自动补的单个哨兵换行
  return out.replace(/\n$/, "");
}

function createFormulaSpan(
  key: string,
  latex: string,
  edited: boolean,
  active: boolean,
  invalid: boolean,
): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = `wysiwyg-formula${active ? " active" : ""}${edited ? " edited" : ""}${invalid ? " invalid" : ""}`;
  span.contentEditable = "false";
  span.dataset.formulaKey = key;
  span.dataset.latex = latex;
  span.tabIndex = 0;
  span.setAttribute("role", "button");
  span.setAttribute("aria-label", `公式 ${key.slice(1)}：${latex}`);

  const html = latex.trim() ? renderLatex(latex.trim()) : null;
  if (html) {
    span.innerHTML = html;
  } else {
    const code = document.createElement("code");
    code.textContent = latex.trim() || `<${key}/>`;
    span.appendChild(code);
  }
  return span;
}

function populateEditorDom(
  root: HTMLElement,
  value: string,
  formulas: SegmentData["formulas"],
  originalFormulas: SegmentData["formulas"],
  activeFormula: string | null,
): void {
  root.innerHTML = "";
  const pieces = tokenize(value);
  for (const piece of pieces) {
    if (piece.kind === "text") {
      const lines = piece.text.split("\n");
      lines.forEach((line, idx) => {
        if (idx > 0) root.appendChild(document.createElement("br"));
        if (line) root.appendChild(document.createTextNode(line));
      });
      continue;
    }
    const latex = formulas[piece.key]?.latex ?? "";
    const orig = (originalFormulas[piece.key]?.latex ?? "").trim();
    const edited = latex.trim() !== orig;
    const invalid = edited && (!latex.trim() || renderLatex(latex.trim()) === null);
    root.appendChild(createFormulaSpan(piece.key, latex, edited, activeFormula === piece.key, invalid));
  }
}

export const InlineFormulaEditor = forwardRef<InlineFormulaEditorHandle, Props>(function InlineFormulaEditor(
  { value, formulas, originalFormulas, activeFormula, onChange, onSelectFormula, onCopySuccess, onCopyError },
  ref,
) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const editRef = useRef<HTMLDivElement>(null);
  const lastSerializedRef = useRef<string | null>(null);
  const composingRef = useRef(false);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [hoverTip, setHoverTip] = useState<HoverTip | null>(null);
  const [tipCopied, setTipCopied] = useState<string | null>(null);

  // 仅在外部 value 真正不同于当前 DOM（如初始挂载、重翻更新、一键补回/还原）时重建 DOM，输入时不碰光标
  useLayoutEffect(() => {
    const root = editRef.current;
    if (!root) return;
    if (lastSerializedRef.current === value) return;
    populateEditorDom(root, value, formulas, originalFormulas, activeFormula);
    lastSerializedRef.current = value;
  }, [value, formulas, originalFormulas, activeFormula]);

  // 当公式 LaTeX 或激活状态变化时，只原地刷新对应原子 span，完全不影响文字节点和输入光标
  useLayoutEffect(() => {
    const root = editRef.current;
    if (!root) return;
    const spans = root.querySelectorAll<HTMLSpanElement>("[data-formula-key]");
    spans.forEach((span) => {
      const key = span.dataset.formulaKey ?? "";
      const latex = formulas[key]?.latex ?? "";
      const orig = (originalFormulas[key]?.latex ?? "").trim();
      const edited = latex.trim() !== orig;
      const invalid = edited && (!latex.trim() || renderLatex(latex.trim()) === null);
      const isActive = activeFormula === key;
      span.className = `wysiwyg-formula${isActive ? " active" : ""}${edited ? " edited" : ""}${invalid ? " invalid" : ""}`;
      if (span.dataset.latex !== latex) {
        span.dataset.latex = latex;
        span.setAttribute("aria-label", `公式 ${key.slice(1)}：${latex}`);
        const html = latex.trim() ? renderLatex(latex.trim()) : null;
        if (html) {
          span.innerHTML = html;
        } else {
          span.textContent = "";
          const code = document.createElement("code");
          code.textContent = latex.trim() || `<${key}/>`;
          span.appendChild(code);
        }
      }
    });
  }, [formulas, originalFormulas, activeFormula]);

  useEffect(() => {
    return () => clearTimeout(hideTimerRef.current);
  }, []);

  const syncFromDom = useCallback(() => {
    const root = editRef.current;
    if (!root) return;
    const next = serializeEditorDom(root);
    if (next !== lastSerializedRef.current) {
      lastSerializedRef.current = next;
      onChange(next);
    }
  }, [onChange]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => editRef.current?.focus(),
      insertFormula: (key: string) => {
        const root = editRef.current;
        if (!root) return;
        root.focus();
        const latex = formulas[key]?.latex ?? "";
        const orig = (originalFormulas[key]?.latex ?? "").trim();
        const edited = latex.trim() !== orig;
        const invalid = edited && (!latex.trim() || renderLatex(latex.trim()) === null);
        const span = createFormulaSpan(key, latex, edited, activeFormula === key, invalid);
        const sel = window.getSelection();
        if (sel && sel.rangeCount > 0 && root.contains(sel.anchorNode)) {
          const range = sel.getRangeAt(0);
          range.deleteContents();
          range.insertNode(span);
          range.setStartAfter(span);
          range.collapse(true);
          sel.removeAllRanges();
          sel.addRange(range);
        } else {
          root.appendChild(span);
        }
        syncFromDom();
      },
    }),
    [formulas, originalFormulas, activeFormula, syncFromDom],
  );

  const showTipFor = (target: HTMLElement) => {
    clearTimeout(hideTimerRef.current);
    const wrap = wrapRef.current;
    const key = target.dataset.formulaKey;
    if (!wrap || !key) return;
    const latex = (formulas[key]?.latex ?? target.dataset.latex ?? "").trim();
    const wrapRect = wrap.getBoundingClientRect();
    const rect = target.getBoundingClientRect();
    const centerX = rect.left - wrapRect.left + rect.width / 2;
    const left = Math.max(8, Math.min(wrapRect.width - 12, centerX));
    const top = Math.max(0, rect.top - wrapRect.top);
    setHoverTip({ key, latex, left, top });
  };

  const scheduleHideTip = () => {
    clearTimeout(hideTimerRef.current);
    hideTimerRef.current = setTimeout(() => setHoverTip(null), 180);
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    const text = e.clipboardData.getData("text/plain");
    e.preventDefault();
    const root = editRef.current;
    if (!root) return;
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0 && root.contains(sel.anchorNode)) {
      const range = sel.getRangeAt(0);
      range.deleteContents();
      const node = document.createTextNode(text);
      range.insertNode(node);
      range.setStartAfter(node);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      root.appendChild(document.createTextNode(text));
    }
    syncFromDom();
  };

  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const formulaEl = (e.target as HTMLElement | null)?.closest<HTMLElement>("[data-formula-key]");
    if (!formulaEl) return;
    const key = formulaEl.dataset.formulaKey;
    if (!key) return;
    e.preventDefault();
    e.stopPropagation();
    setHoverTip(null);
    onSelectFormula(key);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const formulaEl = (e.target as HTMLElement | null)?.closest<HTMLElement>("[data-formula-key]");
    if (formulaEl && (e.key === "Enter" || e.key === " ")) {
      const key = formulaEl.dataset.formulaKey;
      if (key) {
        e.preventDefault();
        onSelectFormula(key);
      }
    }
  };

  const copyFromTip = async (key: string, latex: string) => {
    try {
      await copyFormula(latex, false);
      setTipCopied(key);
      window.setTimeout(() => setTipCopied((cur) => (cur === key ? null : cur)), 1400);
      onCopySuccess?.(key);
    } catch {
      onCopyError?.("复制失败，请检查浏览器的剪贴板权限");
    }
  };

  return (
    <div className="wysiwyg-wrap" ref={wrapRef}>
      <div
        ref={editRef}
        className="wysiwyg-editor"
        contentEditable
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        aria-label="译文"
        data-placeholder="输入或修改中文译文…"
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={() => {
          composingRef.current = false;
          syncFromDom();
        }}
        onInput={() => {
          if (!composingRef.current) syncFromDom();
        }}
        onPaste={handlePaste}
        onCopy={interceptMathSelectionCopy}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        onMouseOver={(e) => {
          const formulaEl = (e.target as HTMLElement | null)?.closest<HTMLElement>("[data-formula-key]");
          if (formulaEl) showTipFor(formulaEl);
        }}
        onMouseOut={(e) => {
          const fromFormula = (e.target as HTMLElement | null)?.closest<HTMLElement>("[data-formula-key]");
          if (!fromFormula) return;
          const toEl = e.relatedTarget as HTMLElement | null;
          if (toEl && fromFormula.contains(toEl)) return;
          scheduleHideTip();
        }}
      />

      {hoverTip && (
        <div
          className="wysiwyg-formula-tooltip"
          role="tooltip"
          style={{ left: hoverTip.left, top: hoverTip.top }}
          onMouseEnter={() => clearTimeout(hideTimerRef.current)}
          onMouseLeave={scheduleHideTip}
        >
          <button
            type="button"
            className="wysiwyg-tooltip-btn"
            onClick={(e) => {
              e.stopPropagation();
              void copyFromTip(hoverTip.key, hoverTip.latex);
            }}
          >
            {tipCopied === hoverTip.key ? "已复制" : "复制"}
          </button>
          <button
            type="button"
            className="wysiwyg-tooltip-btn primary-text"
            onClick={(e) => {
              e.stopPropagation();
              const k = hoverTip.key;
              setHoverTip(null);
              onSelectFormula(k);
            }}
          >
            修改
          </button>
        </div>
      )}
    </div>
  );
});
