/**
 * 把一个段落的译文按各块原文长度拆开（跨栏/跨页段落），优先在标点处断开，不拆开公式占位符。
 * 拆分方法与 spikes/s5b_mineru_overlay.py 中验证过的一致。
 */

const BREAK_AFTER = new Set(["，", "。", "；", "：", "、", "）", "！", "？", ",", ";", ":", ".", ")"]);
const SEARCH = 40;

function insidePlaceholder(text: string, pos: number): boolean {
  const open = text.lastIndexOf("<", pos - 1);
  return open !== -1 && text.indexOf(">", open) >= pos;
}

function cutNear(text: string, target: number): number {
  for (let d = 0; d <= SEARCH; d++) {
    for (const pos of [target - d, target + d]) {
      if (pos > 0 && pos < text.length && BREAK_AFTER.has(text[pos - 1] ?? "") && !insidePlaceholder(text, pos)) {
        return pos;
      }
    }
  }
  // 附近没有标点：退回目标位置，但避开占位符
  let pos = Math.min(Math.max(target, 1), text.length - 1);
  while (pos < text.length && insidePlaceholder(text, pos)) pos++;
  return pos;
}

export function splitByParts(text: string, partChars: number[]): string[] {
  if (partChars.length <= 1) return [text];
  const total = partChars.reduce((a, b) => a + b, 0) || 1;
  const pieces: string[] = [];
  let start = 0;
  let cumulative = 0;
  for (let i = 0; i < partChars.length - 1; i++) {
    cumulative += partChars[i] ?? 0;
    // 第 i 块结束位置 ≈ 译文总长 × 前 i 块原文占比
    const target = Math.round((text.length * cumulative) / total);
    const cut = start >= text.length ? text.length : cutNear(text, Math.max(target, start + 1));
    pieces.push(text.slice(start, cut));
    start = cut;
  }
  pieces.push(text.slice(start));
  return pieces;
}

/** 把含 <mN/> 占位符的文字切成文字片段与公式片段。 */
export type Piece = { kind: "text"; text: string } | { kind: "formula"; key: string };

export function tokenize(text: string): Piece[] {
  const out: Piece[] = [];
  const re = /<(m\d+)\/>/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push({ kind: "text", text: text.slice(last, m.index) });
    out.push({ kind: "formula", key: m[1] ?? "" });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}

/**
 * 文字里允许的格式标签（MinerU 用 <sup>/<sub> 表示上下标，译文会原样保留）。
 * 解析成树后由调用方渲染为真实元素；白名单外的标签当作普通文字。
 */
export type RichNode = string | { tag: RichTag; children: RichNode[] };
export type RichTag = "sup" | "sub" | "b" | "i";
const RICH_TAGS = new Set<string>(["sup", "sub", "b", "i"]);

export function parseRich(text: string): RichNode[] {
  const root: RichNode[] = [];
  const stack: { tag: RichTag; children: RichNode[] }[] = [];
  const top = () => stack[stack.length - 1]?.children ?? root;
  const re = /<(\/?)([a-z]+)>/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const [whole, close, tag = ""] = m;
    if (!RICH_TAGS.has(tag)) continue; // 不认识的标签按文字处理
    if (m.index > last) top().push(text.slice(last, m.index));
    last = m.index + whole.length;
    if (!close) {
      const node = { tag: tag as RichTag, children: [] as RichNode[] };
      top().push(node);
      stack.push(node);
    } else if (stack.length && stack[stack.length - 1]?.tag === tag) {
      stack.pop();
    }
    // 不配对的结束标签直接丢弃
  }
  if (last < text.length) top().push(text.slice(last));
  return root;
}
