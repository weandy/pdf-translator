/**
 * 在一块译文里找链接文字的位置（与后端 rendering/links.py 的“紧凑文本”约定一致：忽略所有空白）。
 * 后端已决定每个链接对应哪段文字、是第几处（nth），这里只负责在渲染用的若干段文字里找到它。
 */

export interface LinkTarget {
  text: string; // 紧凑文字（不含空白）
  nth: number; // 第几处，从 0 起
}

export interface LinkRange {
  start: number;
  end: number;
  link: number; // targets 里的下标
}

/** 返回每段文字里要包成链接的区间。一处链接跨了两段文字时，两段各包一部分。 */
export function linkRanges(leaves: string[], targets: LinkTarget[]): LinkRange[][] {
  const where: [number, number][] = []; // 紧凑文本第 k 个字 → (第几段, 段内位置)
  leaves.forEach((leaf, li) => {
    for (let i = 0; i < leaf.length; i++) if (!/\s/.test(leaf[i] ?? "")) where.push([li, i]);
  });
  const compact = where.map(([li, i]) => leaves[li]?.[i] ?? "").join("");
  const out: LinkRange[][] = leaves.map(() => []);
  const taken = new Set<number>();
  targets.forEach((t, link) => {
    if (!t.text) return;
    let pos = -1;
    for (let k = 0; k <= t.nth; k++) {
      pos = compact.indexOf(t.text, pos + 1);
      if (pos < 0) return;
    }
    const chars = where.slice(pos, pos + t.text.length);
    if (chars.some((_, k) => taken.has(pos + k))) return;
    chars.forEach((_, k) => taken.add(pos + k));
    for (const li of new Set(chars.map(([l]) => l))) {
      const idx = chars.filter(([l]) => l === li).map(([, i]) => i);
      out[li]?.push({ start: idx[0] ?? 0, end: (idx[idx.length - 1] ?? 0) + 1, link });
    }
  });
  out.forEach((r) => r.sort((a, b) => a.start - b.start));
  return out;
}

/** 紧凑文字在一段文字里出现的次数（图注编号单独渲染时，用来扣掉它里面的出现次数）。 */
export function countCompact(text: string, needle: string): number {
  if (!needle) return 0;
  const compact = text.replace(/\s+/g, "");
  let n = 0;
  for (let i = compact.indexOf(needle); i >= 0; i = compact.indexOf(needle, i + 1)) n++;
  return n;
}
