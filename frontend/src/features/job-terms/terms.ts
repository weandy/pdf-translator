/**
 * 本文术语：用浏览器里最新的术语库覆盖服务器按建任务时算出的译法，并检查每段译文有没有用上推荐译法。
 */
import { findTerm, type TermEntry } from "@/entities/glossary/model";
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
  mismatchedIds: string[]; // 已翻译、但译文里没有推荐译法的段落
}

const ALTERNATIVES = /[；;/|、，,]/;
const BRACKETS = /[（(][^）)]*[）)]/g;
const squash = (s: string) => s.replace(/\s+/g, "").toLowerCase();

/** 译文里有没有用这个译法：译法有多个写法（“A；B”）时任一即可，括号里的注释与空白不计。 */
export function renders(target: string, translation: string): boolean {
  const text = squash(translation);
  return target
    .split(ALTERNATIVES)
    .map((v) => squash(v.replace(BRACKETS, "")))
    .some((v) => v.length > 0 && text.includes(v));
}

export function termRows(uses: TermUse[], mine: TermEntry[], segments: SegmentData[]): TermRow[] {
  const byId = new Map(segments.map((s) => [s.id, s]));
  const rows = uses.map((u) => {
    const own = findTerm(u.source, mine);
    const target = own?.target ?? u.target;
    const mismatchedIds = u.segment_ids.filter((id) => {
      const seg = byId.get(id);
      return (
        seg !== undefined && (seg.status === "done" || seg.status === "edited") && !renders(target, seg.target ?? "")
      );
    });
    return {
      source: u.source,
      target,
      origin: own ? "user" : u.origin,
      changed: own !== undefined && own.target !== u.target,
      segmentIds: u.segment_ids,
      mismatchedIds,
    } satisfies TermRow;
  });
  return rows.sort(
    (a, b) =>
      b.mismatchedIds.length - a.mismatchedIds.length ||
      b.segmentIds.length - a.segmentIds.length ||
      a.source.localeCompare(b.source),
  );
}
