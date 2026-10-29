/**
 * 渲染含 <mN/> 占位符的译文：公式用 KaTeX 渲染，失败时用原文位置的裁剪图兜底（docs/01-tech-stack.md §5.2）。
 */
import katex from "katex";
import "katex/dist/katex.min.css";
import { memo, type ReactNode } from "react";

import { linkRanges, type LinkTarget } from "@/shared/text/links";
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

/** 译文里的链接：网址（href）在新标签页打开，页内跳转（onActivate）由阅读器滚动过去。 */
export interface InlineLink extends LinkTarget {
  href?: string;
  onActivate?: () => void;
}

interface Props {
  text: string;
  /** bbox 为 null：公式没有原图位置（扫描页），渲染失败时显示 LaTeX 原文 */
  formulas: Record<string, { latex: string; bbox?: unknown }>;
  fallbackUrl: (key: string) => string;
  links?: InlineLink[];
}

function anchor(link: InlineLink | undefined, children: ReactNode[], key: string): ReactNode {
  if (!link) return children;
  return (
    <a
      key={key}
      className="ref-link"
      href={link.href ?? "#"}
      target={link.href ? "_blank" : undefined}
      rel="noreferrer"
      onClick={(e) => {
        e.stopPropagation(); // 不触发段落选中
        if (!link.href) {
          e.preventDefault();
          link.onActivate?.();
        }
      }}
    >
      {children}
    </a>
  );
}

/** 长词（如 Python/OR-Tools）在 / 之后允许断行（与导出 PDF 一致；浏览器默认只在连字符后断）。 */
const SLASH_BREAK = /(?<=[A-Za-z0-9]\/)(?=[A-Za-z0-9])/;

function latin(text: string, key: string): ReactNode[] {
  return text.split(SLASH_BREAK).flatMap((piece, i) => (i ? [<wbr key={`${key}w${i}`} />, piece] : [piece]));
}

/** 中文片段包进 span.zh，由样式指定中文字体（西文用外层字体）。 */
function scripts(text: string, key: string): ReactNode[] {
  return splitScript(text).flatMap((run, i) =>
    run.cjk ? (
      <span key={`${key}:${i}`} className="zh">
        {run.text}
      </span>
    ) : (
      latin(run.text, `${key}:${i}`)
    ),
  );
}

type Leaf = (text: string, key: string) => ReactNode[];

function rich(nodes: RichNode[], key: string, leaf: Leaf = scripts): ReactNode[] {
  return nodes.flatMap((n, i) => {
    if (typeof n === "string") return leaf(n, `${key}.${i}`);
    const Tag = n.tag;
    return <Tag key={`${key}.${i}`}>{rich(n.children, `${key}.${i}`, leaf)}</Tag>;
  });
}

function collect(nodes: RichNode[], out: string[]): void {
  for (const n of nodes) {
    if (typeof n === "string") out.push(n);
    else collect(n.children, out);
  }
}

export const InlineMathText = memo(function InlineMathText({ text, formulas, fallbackUrl, links }: Props) {
  const pieces = tokenize(text).map((piece) => ({ piece, rich: piece.kind === "text" ? parseRich(piece.text) : [] }));
  // 先按渲染顺序收集文字叶子，算出链接落在哪几段文字的哪几个字上，再逐段渲染
  const leaves: string[] = [];
  pieces.forEach((p) => collect(p.rich, leaves));
  const ranges = links?.length ? linkRanges(leaves, links) : null;
  let next = 0;
  const leaf: Leaf = (s, key) => {
    const rs = ranges?.[next++] ?? [];
    if (!rs.length) return scripts(s, key);
    const out: ReactNode[] = [];
    let at = 0;
    rs.forEach((r, j) => {
      if (r.start > at) out.push(...scripts(s.slice(at, r.start), `${key}t${j}`));
      out.push(anchor(links?.[r.link], scripts(s.slice(r.start, r.end), `${key}a${j}`), `${key}l${j}`));
      at = r.end;
    });
    if (at < s.length) out.push(...scripts(s.slice(at), `${key}t`));
    return out;
  };
  const nodes: ReactNode[] = pieces.map(({ piece, rich: parsed }, i) => {
    if (piece.kind === "text") return rich(parsed, String(i), leaf);
    const formula = formulas[piece.key];
    const latex = formula?.latex;
    const html = latex ? renderLatex(latex) : null;
    if (html) return <span key={i} className="math" dangerouslySetInnerHTML={{ __html: html }} />;
    if (latex && formula?.bbox === null) {
      return (
        <code key={i} className="math-raw">
          {latex}
        </code>
      );
    }
    return <img key={i} className="math-fallback" src={fallbackUrl(piece.key)} alt={latex ?? piece.key} />;
  });
  return <>{nodes}</>;
});
