/**
 * 渲染含 <mN/> 占位符的译文：公式用 KaTeX 渲染，失败时用原文位置的裁剪图兜底（docs/01-tech-stack.md §5.2）。
 */
import katex from "katex";
import "katex/dist/katex.min.css";
import { memo, type ReactNode } from "react";

import { splitScript } from "@/shared/text/script";
import { parseRich, tokenize, type RichNode } from "@/shared/text/split";

const cache = new Map<string, string | null>();

/** 返回 KaTeX 生成的 HTML；LaTeX 有误时返回 null。 */
export function renderLatex(latex: string): string | null {
  const hit = cache.get(latex);
  if (hit !== undefined) return hit;
  let html: string | null;
  try {
    html = katex.renderToString(latex, { throwOnError: true, displayMode: false, output: "html", strict: "ignore" });
  } catch {
    html = null;
  }
  cache.set(latex, html);
  return html;
}

interface Props {
  text: string;
  formulas: Record<string, { latex: string }>;
  fallbackUrl: (key: string) => string;
}

/** 中文片段包进 span.zh，由样式指定中文字体（西文用外层字体）。 */
function scripts(text: string, key: string): ReactNode[] {
  return splitScript(text).map((run, i) =>
    run.cjk ? (
      <span key={`${key}:${i}`} className="zh">
        {run.text}
      </span>
    ) : (
      run.text
    ),
  );
}

function rich(nodes: RichNode[], key: string): ReactNode[] {
  return nodes.flatMap((n, i) => {
    if (typeof n === "string") return scripts(n, `${key}.${i}`);
    const Tag = n.tag;
    return <Tag key={`${key}.${i}`}>{rich(n.children, `${key}.${i}`)}</Tag>;
  });
}

export const InlineMathText = memo(function InlineMathText({ text, formulas, fallbackUrl }: Props) {
  const nodes: ReactNode[] = tokenize(text).map((piece, i) => {
    if (piece.kind === "text") return rich(parseRich(piece.text), String(i));
    const latex = formulas[piece.key]?.latex;
    const html = latex ? renderLatex(latex) : null;
    if (html) return <span key={i} className="math" dangerouslySetInnerHTML={{ __html: html }} />;
    return <img key={i} className="math-fallback" src={fallbackUrl(piece.key)} alt={latex ?? piece.key} />;
  });
  return <>{nodes}</>;
});
