/**
 * 含公式文本的双格式剪贴板复制工具：
 * - text/plain：输出标准 LaTeX（$...$ / $$...$$），适配 Markdown / Obsidian / Notion / Overleaf；
 * - text/html：输出 W3C 标准 MathML，粘贴到 Microsoft Word / WPS 自动变为可编辑原生公式。
 */
import { renderMathml } from "@/shared/math/inline";
import { tokenize } from "@/shared/text/split";

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function wrapTex(latex: string, display = false): string {
  return display ? `$$${latex}$$` : `$${latex}$`;
}

async function writeDualClipboard(plain: string, html: string | null): Promise<void> {
  const clip = navigator.clipboard;
  if (!clip) throw new Error("clipboard unavailable");
  if (html && typeof ClipboardItem !== "undefined" && typeof clip.write === "function") {
    try {
      const item = new ClipboardItem({
        "text/plain": new Blob([plain], { type: "text/plain" }),
        "text/html": new Blob([html], { type: "text/html" }),
      });
      await clip.write([item]);
      return;
    } catch {
      // 部分受限环境不允许 ClipboardItem 写入 text/html，自动回退纯文本 LaTeX
    }
  }
  await clip.writeText(plain);
}

/** 复制单个公式（标准 $...$/$$...$$ + Word MathML）。 */
export async function copyFormula(latex: string, display = false): Promise<void> {
  const cleaned = latex.trim();
  const plain = cleaned ? wrapTex(cleaned, display) : "";
  const mathml = cleaned ? renderMathml(cleaned, display) : null;
  await writeDualClipboard(plain, mathml);
}

/** 复制含 <mN/> 占位符的段落文本，自动将公式还原为标准 LaTeX 与 Word MathML。 */
export async function copyTextWithFormulas(
  text: string,
  formulas: Record<string, { latex: string }>,
): Promise<void> {
  const pieces = tokenize(text);
  let hasFormula = false;
  const plainParts: string[] = [];
  const htmlParts: string[] = [];

  for (const piece of pieces) {
    if (piece.kind === "text") {
      // 去掉译文内部的轻量富文本标签用于纯文本复制
      const cleanPlain = piece.text.replace(/<\/?(?:b|i|sup|sub)>/g, "");
      plainParts.push(cleanPlain);
      htmlParts.push(escapeHtml(cleanPlain));
      continue;
    }
    const latex = formulas[piece.key]?.latex?.trim();
    if (!latex) {
      plainParts.push(`<${piece.key}/>`);
      htmlParts.push(escapeHtml(`<${piece.key}/>`));
      continue;
    }
    hasFormula = true;
    const tex = wrapTex(latex, false);
    plainParts.push(tex);
    const mathml = renderMathml(latex, false);
    htmlParts.push(mathml ?? escapeHtml(tex));
  }

  const plain = plainParts.join("");
  const html = hasFormula ? `<span>${htmlParts.join("")}</span>` : null;
  await writeDualClipboard(plain, html);
}

/** 拦截 DOM 选区复制事件，将其中的 [data-latex] 节点还原为标准 LaTeX 与 MathML。 */
export function interceptMathSelectionCopy(e: React.ClipboardEvent<HTMLElement>): boolean {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
  const range = selection.getRangeAt(0);
  const plainFragment = range.cloneContents();
  const htmlFragment = range.cloneContents();
  const mathNodes = plainFragment.querySelectorAll<HTMLElement>("[data-latex]");
  const ancestorEl =
    range.commonAncestorContainer instanceof Element
      ? range.commonAncestorContainer
      : range.commonAncestorContainer.parentElement;
  const enclosingMath = ancestorEl?.closest<HTMLElement>("[data-latex]");
  if (mathNodes.length === 0 && !enclosingMath) return false;

  e.preventDefault();
  if (enclosingMath && mathNodes.length === 0) {
    const latex = enclosingMath.dataset.latex ?? "";
    const display = enclosingMath.dataset.display === "true";
    e.clipboardData.setData("text/plain", latex ? wrapTex(latex, display) : selection.toString());
    const mathml = latex ? renderMathml(latex, display) : null;
    if (mathml) e.clipboardData.setData("text/html", mathml);
    return true;
  }

  mathNodes.forEach((node) => {
    const latex = node.dataset.latex ?? "";
    const display = node.dataset.display === "true";
    node.replaceWith(document.createTextNode(latex ? wrapTex(latex, display) : ""));
  });
  const plain = plainFragment.textContent ?? selection.toString();
  e.clipboardData.setData("text/plain", plain);

  const htmlMathNodes = htmlFragment.querySelectorAll<HTMLElement>("[data-latex]");
  htmlMathNodes.forEach((node) => {
    const latex = node.dataset.latex ?? "";
    const display = node.dataset.display === "true";
    const mathml = latex ? renderMathml(latex, display) : null;
    if (mathml) {
      const wrapper = document.createElement("span");
      wrapper.innerHTML = mathml;
      node.replaceWith(wrapper.firstChild ?? document.createTextNode(wrapTex(latex, display)));
    } else {
      node.replaceWith(document.createTextNode(latex ? wrapTex(latex, display) : ""));
    }
  });
  const holder = document.createElement("div");
  holder.appendChild(htmlFragment);
  if (holder.innerHTML) e.clipboardData.setData("text/html", holder.innerHTML);
  return true;
}
