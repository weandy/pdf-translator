/**
 * 把一个段落的译文按各块原文长度拆开（跨栏/跨页段落），优先在标点处断开，不拆开公式占位符。
 * 权重、舍入及行首禁则与后端 rendering/export.py 一致。
 */

const BREAK_AFTER = new Set(["，", "。", "；", "：", "、", "）", "！", "？", ",", ";", ":", ".", ")"]);
const NO_START = new Set("，。、；：？！）》」』】〕〉”’…—,.;:?!)]}%");
const SEARCH = 40;

function cutNear(atoms: RegExpMatchArray[], start: number, target: number, limit: number): number {
  for (let d = 0; d <= SEARCH; d++) {
    for (const pos of [target - d, target + d]) {
      if (pos > start && pos <= limit && BREAK_AFTER.has(atoms[pos - 1]?.[0] ?? "")) {
        return pos;
      }
    }
  }
  return Math.min(Math.max(target, start + 1), limit);
}

export function splitByParts(text: string, partChars: number[]): string[] {
  if (partChars.length <= 1) return [text];
  const atoms = [...text.matchAll(/<\/?(?:b|i|sup|sub)>|<m\d+\/>|[\s\S]/gu)]
    .filter((atom) => !/^<\/?(?:b|i|sup|sub)>$/.test(atom[0]));
  const closingTags = /(?:<\/(?:b|i|sup|sub)>)*/y;
  const total = partChars.reduce((a, b) => a + b, 0) || 1;
  const pieces: string[] = [];
  let start = 0;
  let offset = 0;
  let cumulative = 0;
  for (let i = 0; i < partChars.length - 1; i++) {
    cumulative += partChars[i] ?? 0;
    if (start >= atoms.length) {
      pieces.push(text.slice(offset));
      offset = text.length;
      continue;
    }
    const target = Math.floor((2 * atoms.length * cumulative + total) / (2 * total));
    const limit = Math.max(start + 1, atoms.length - (partChars.length - i - 1));
    let cut = cutNear(atoms, start, Math.min(Math.max(target, start + 1), limit), limit);
    const last = atoms[cut - 1]!;
    let end = last.index! + last[0].length;
    for (;;) {
      closingTags.lastIndex = end;
      end = closingTags.exec(text) ? closingTags.lastIndex : end;
      const next = atoms[cut];
      if (!next || next.index !== end || !NO_START.has(next[0])) break;
      end += next[0].length;
      cut++;
    }
    pieces.push(text.slice(offset, end));
    offset = end;
    start = cut;
  }
  pieces.push(text.slice(offset));
  return balanceRichParts(pieces);
}

/** 每个分片各自关闭/重开白名单样式，既保留跨页样式，也不把样式泄漏到下一段。 */
export function balanceRichParts(parts: string[]): string[] {
  const active: string[] = [];
  return parts.map((part) => {
    const prefix = active.map((tag) => `<${tag}>`).join("");
    for (const match of part.matchAll(/<(\/?)(sup|sub|b|i)>/g)) {
      const tag = match[2]!;
      if (!match[1]) active.push(tag);
      else if (active[active.length - 1] === tag) active.pop();
    }
    // 纯标签片段只改变后续样式栈，不制造没有文字的粗体节点。
    if (!part.replace(/<\/?(?:sup|sub|b|i)>/g, "")) return "";
    return (
      prefix +
      part +
      [...active]
        .reverse()
        .map((tag) => `</${tag}>`)
        .join("")
    );
  });
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
