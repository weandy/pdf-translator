/**
 * 本文术语：用浏览器里最新的术语库覆盖服务器按建任务时算出的译法，并检查每段译文是否包含推荐译法的字面文本。
 */
import { findTermMatching, type TermEntry } from "@/entities/glossary/model";
import type { SegmentData, TermUse } from "@/shared/api/client";

export type Origin = TermUse["origin"];

export const ORIGIN_LABELS: Record<Origin, string> = {
  user: "我的术语库",
  paper: "本文自动抽取",
  library: "公共词库",
};

export interface TermRow {
  source: string;
  target: string; // 现在的推荐译法（我的术语库优先）
  origin: Origin;
  changed: boolean; // 建任务后在我的术语库里改过译法
  segmentIds: string[];
  mismatchedIds: string[]; // 已翻译、但译文里没有推荐译法字面文本的段落
}

const ALTERNATIVES = /[；;|、，,]/;
const BRACKETS = /[（(][^）)]*[）)]/g;
const STYLE = /<\/?(?:sup|sub|b|i)>/gi;
const FORMULA = /<m\d+\/>/gi;
const ASCII_ALNUM = /[A-Za-z0-9]/;
const ASCII_WORD = /[A-Za-z0-9_]/;
const CJK_SPACE = /([\u3400-\u4dbf\u4e00-\u9fff]) | (?=[\u3400-\u4dbf\u4e00-\u9fff])/g;

function visibleText(text: string): string {
  return text
    .replace(STYLE, "")
    .replace(FORMULA, "\0")
    .replace(/\s+/g, " ")
    .replace(CJK_SPACE, "$1")
    .trim()
    .toLowerCase();
}

function alternatives(target: string): string[] {
  const clean = target.replace(BRACKETS, "");
  // 仅两侧都不接 ASCII 字母数字的斜杠充当分隔符。
  const separated = clean.replace(/\//g, (slash: string, index: number) =>
    ASCII_ALNUM.test(clean.charAt(index - 1)) || ASCII_ALNUM.test(clean.charAt(index + 1)) ? slash : ";",
  );
  return separated
    .split(ALTERNATIVES)
    .map(visibleText)
    .filter((variant) => variant.length > 0 && !variant.includes("\0"));
}

interface Span {
  start: number;
  end: number;
}

function* occurrences(text: string, variants: string[]): Generator<Span> {
  for (const variant of variants) {
    const leftBoundary = ASCII_WORD.test(variant.charAt(0));
    const rightBoundary = ASCII_WORD.test(variant.charAt(variant.length - 1));
    for (let start = text.indexOf(variant); start !== -1; start = text.indexOf(variant, start + 1)) {
      const end = start + variant.length;
      if (
        (!leftBoundary || !ASCII_WORD.test(text.charAt(start - 1))) &&
        (!rightBoundary || !ASCII_WORD.test(text.charAt(end)))
      ) {
        yield { start, end };
      }
    }
  }
}

/** 多个译法中只需命中一个，英文词形保留词界。 */
export function renders(target: string, translation: string): boolean {
  return !occurrences(visibleText(translation), alternatives(target)).next().done;
}

export function termRows(uses: TermUse[], mine: TermEntry[], segments: SegmentData[]): TermRow[] {
  const byId = new Map<string, { row: TermRow; variants: string[] }[]>();
  const rows = uses.map((u) => {
    const own = findTermMatching(u.source, mine);
    const target = own?.target ?? u.target;
    const row: TermRow = {
      source: u.source,
      target,
      origin: own ? "user" : u.origin,
      changed: own !== undefined && own.target !== u.target,
      segmentIds: u.segment_ids,
      mismatchedIds: [],
    };
    const item = { row, variants: alternatives(target) };
    for (const id of u.segment_ids) {
      const items = byId.get(id);
      if (items) items.push(item);
      else byId.set(id, [item]);
    }
    return row;
  });
  for (const segment of segments) {
    const items = byId.get(segment.id);
    if (!items || (segment.status !== "done" && segment.status !== "edited")) continue;
    const text = visibleText(segment.target ?? "");
    const hits = items.map(({ row, variants }) => ({ row, spans: [...occurrences(text, variants)] }));
    for (const { row, spans } of hits) {
      const adopted = spans.some(({ start, end }) =>
        hits.every(
          (other) =>
            other.row.source === row.source ||
            !other.spans.some(
              (outer) => outer.start <= start && end <= outer.end && outer.end - outer.start > end - start,
            ),
        ),
      );
      if (!adopted) row.mismatchedIds.push(segment.id);
    }
  }
  for (const row of rows) {
    if (row.mismatchedIds.length < 2) continue;
    const mismatches = new Set(row.mismatchedIds);
    row.mismatchedIds = row.segmentIds.filter((id) => mismatches.has(id));
  }
  return rows.sort(
    (a, b) =>
      b.mismatchedIds.length - a.mismatchedIds.length ||
      b.segmentIds.length - a.segmentIds.length ||
      a.source.localeCompare(b.source),
  );
}
