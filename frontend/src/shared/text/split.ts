/**
 * 把跨栏或跨页译文按各块原文长度拆开，优先在标点处断开并保留复合原子。
 * 权重、舍入及行首禁则与后端 rendering/export.py 一致。
 */

const BREAK_AFTER = new Set(["，", "。", "；", "：", "、", "）", "！", "？", ",", ";", ":", ".", ")"]);
const NO_START = new Set("，。、；：？！）》」』】〕〉”’…—,.;:?!)]}%");
const SEARCH = 40;
const CITATION_MAX = 40;
const CITATION_CHARS = new Set(" ,–—-;，、；");
const ASCII_DIGITS = new Set("0123456789");
const ASCII_ALNUM = /^[A-Za-z0-9]$/;
const URL_CHAR = /^[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]$/;
const URL_TAIL = new Set(".,;:!?");

type Atom = { start: number; end: number; char: string };

function citationEnd(chars: string[], start: number): number {
  const close = chars[start] === "[" ? "]" : "］";
  let hasDigit = false;
  for (let end = start + 1; end < Math.min(chars.length, start + CITATION_MAX); end++) {
    const char = chars[end]!;
    if (char === close) return hasDigit ? end + 1 : start;
    if (ASCII_DIGITS.has(char)) hasDigit = true;
    else if (!CITATION_CHARS.has(char)) break;
  }
  return start;
}

function urlEnd(chars: string[], start: number): number {
  if (start > 0 && ASCII_ALNUM.test(chars[start - 1]!)) return start;
  const prefix = chars.slice(start, start + 8).join("").toLowerCase();
  let head: number;
  if (prefix.startsWith("https://")) head = start + 8;
  else if (prefix.startsWith("http://")) head = start + 7;
  else if (prefix.startsWith("www.")) head = start + 4;
  else return start;

  // 记录配对的右括号最远位置，剥除尾部时可区分 URL 内的括号与外部括号。
  let parens = 0;
  let brackets = 0;
  let pairedParen = -1;
  let query = -1;
  let fragment = -1;
  const pairedBrackets: number[] = [];
  let end = head;
  for (; end < chars.length && URL_CHAR.test(chars[end]!); end++) {
    const char = chars[end];
    if (char === "(") parens++;
    else if (char === ")" && parens) {
      parens--;
      pairedParen = end;
    } else if (char === "[") brackets++;
    else if (char === "]" && brackets) {
      brackets--;
      pairedBrackets.push(end);
    } else if (char === "?" && query < 0 && fragment < 0) query = end;
    else if (char === "#" && fragment < 0) fragment = end;
  }

  while (end > head) {
    const tail = end - 1;
    const char = chars[tail]!;
    if (URL_TAIL.has(char) || (char === ")" && tail > pairedParen)) {
      end--;
    } else if (char === "]") {
      if (tail > (pairedBrackets.at(-1) ?? -1)) {
        end--;
        continue;
      }
      let opening = tail - 1;
      while (opening > head && tail - opening < CITATION_MAX && chars[opening] !== "[") opening--;
      if (
        opening <= head ||
        (query >= 0 && query < opening && (fragment < 0 || opening < fragment)) ||
        chars[opening] !== "[" ||
        citationEnd(chars, opening) !== end
      ) break;
      end = opening;
      pairedBrackets.pop();
    } else {
      break;
    }
  }
  return end > head ? end : start;
}

function numberEnd(chars: string[], start: number): [end: number, decimal: boolean] {
  // 单趟消费数字串，即使没有小数点也无需从每个数字重新查找。
  let end = start + Number(chars[start] === "v" || chars[start] === "V");
  let decimal = false;
  for (;;) {
    while (end < chars.length && ASCII_DIGITS.has(chars[end]!)) end++;
    if (end + 1 >= chars.length || chars[end] !== "." || !ASCII_DIGITS.has(chars[end + 1]!)) {
      return [end, decimal];
    }
    decimal = true;
    end++;
  }
}

function splitAtoms(text: string): { atoms: Atom[]; prefix: number[] } {
  const units = [...text.matchAll(/<\/?(?:b|i|sup|sub)>|<m[0-9]+\/>|[\s\S]/gu)]
    .filter((unit) => !/^<\/?(?:b|i|sup|sub)>$/.test(unit[0]));
  const chars = units.map((unit) => unit[0]);
  const atoms: Atom[] = [];
  const prefix = [0];
  const append = (start: number, end: number) => {
    const last = units[end - 1]!;
    atoms.push({
      start: units[start]!.index!,
      end: last.index! + last[0].length,
      char: end === start + 1 ? chars[start]! : "",
    });
    prefix.push(prefix[prefix.length - 1]! + end - start);
  };

  let start = 0;
  while (start < units.length) {
    const char = chars[start]!;
    let end = start;
    if (char === "h" || char === "H" || char === "w" || char === "W") {
      end = urlEnd(chars, start);
    } else if (char === "[" || char === "［") {
      end = citationEnd(chars, start);
    } else if (ASCII_DIGITS.has(char) || ((char === "v" || char === "V") && ASCII_DIGITS.has(chars[start + 1] ?? ""))) {
      let decimal: boolean;
      [end, decimal] = numberEnd(chars, start);
      if (!decimal) {
        for (let i = start; i < end; i++) append(i, i + 1);
        start = end;
        continue;
      }
    }
    end = Math.max(start + 1, end);
    append(start, end);
    start = end;
  }
  return { atoms, prefix };
}

function cutNear(atoms: Atom[], start: number, target: number, limit: number): number {
  for (let d = 0; d <= SEARCH; d++) {
    for (const pos of [target - d, target + d]) {
      if (pos > start && pos <= limit && BREAK_AFTER.has(atoms[pos - 1]!.char)) {
        return pos;
      }
    }
  }
  return target;
}

export function splitByParts(text: string, partChars: number[]): string[] {
  if (partChars.length <= 1) return [text];
  const { atoms, prefix } = splitAtoms(text);
  const closingTags = /(?:<\/(?:b|i|sup|sub)>)*/y;
  const total = partChars.reduce((a, b) => a + b, 0) || 1;
  const visible = prefix[prefix.length - 1]!;
  const exactWeights = (2 * Math.max(1, visible) + 1) * total > Number.MAX_SAFE_INTEGER
    ? partChars.map((weight) => BigInt(weight))
    : null;
  const exactTotal = exactWeights?.reduce((a, b) => a + b, 0n) || 1n;
  const pieces: string[] = [];
  let start = 0;
  let offset = 0;
  let cumulative = 0;
  let exactCumulative = 0n;
  let nearest = 0;
  for (let i = 0; i < partChars.length - 1; i++) {
    cumulative += partChars[i] ?? 0;
    if (start >= atoms.length) {
      pieces.push(text.slice(offset));
      offset = text.length;
      continue;
    }
    let target: number;
    if (exactWeights) {
      exactCumulative += exactWeights[i]!;
      target = Number((2n * BigInt(visible) * exactCumulative + exactTotal) / (2n * exactTotal));
    } else {
      target = Math.floor((2 * visible * cumulative + total) / (2 * total));
    }
    // 目标与合法索引区间单调前移。同距保留左边界，给剩余块预留位置。
    const limit = Math.max(start + 1, atoms.length - (partChars.length - i - 1));
    nearest = Math.max(nearest, start + 1);
    while (nearest < limit && prefix[nearest]! + prefix[nearest + 1]! < 2 * target) nearest++;
    let cut = cutNear(atoms, start, nearest, limit);
    let end = atoms[cut - 1]!.end;
    for (;;) {
      closingTags.lastIndex = end;
      end = closingTags.exec(text) ? closingTags.lastIndex : end;
      const next = atoms[cut];
      if (!next || next.start !== end || !NO_START.has(next.char)) break;
      end = next.end;
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
    const visible = Boolean(part.replace(/<\/?(?:sup|sub|b|i)>/g, ""));
    const prefix = visible ? active.map((tag) => `<${tag}>`).join("") : "";
    for (const match of part.matchAll(/<(\/?)(sup|sub|b|i)>/g)) {
      const tag = match[2]!;
      if (!match[1]) active.push(tag);
      else if (active[active.length - 1] === tag) active.pop();
    }
    // 纯标签片段只改变后续样式栈，不制造没有文字的粗体节点。
    if (!visible) return "";
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
