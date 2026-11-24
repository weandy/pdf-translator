/**
 * 阅读器数据的派生结构：每个块要显示的文字（跨栏/跨页段落的译文按各块原文长度拆开）。
 */
import type { BlockData, DocumentData, SegmentData } from "@/shared/api/client";
import { splitByParts } from "@/shared/text/split";

export interface BlockText {
  block: BlockData;
  segment: SegmentData;
  text: string; // 译文片段；没有译文（失败/未完成）时为原文片段
  sourceText: string; // 原文片段，用于阅读器的可选择文字层
  renderBox?: BlockData["bbox"]; // 同栏连续块共用的排版区域
  translated: boolean;
  first: boolean; // 段落的第一部分（跨栏/跨页段落拆开后）
}

/** 排版角色决定字体（GB/T 7713.2 附录 B），与后端 rendering/export.py 的 role() 一致。 */
export type Role = "heading" | "caption" | "body" | "label";

export function roleOf(block: BlockData, translated = true): Role {
  if (block.type === "figure_text") return "label"; // 图里的文字：黑体、居中
  if (block.type === "title" || (!translated && block.bold)) return "heading";
  if (block.type === "caption") return "caption";
  return "body";
}

/** 与导出 PDF 一致的行距（rendering/export.py 的 LINE_HEIGHT）。 */
export const LINE_HEIGHT = 1.2;
const COLUMN_MIN_GAP = 8;
const BOTTOM_MARGIN = 4;
const SECTION_HEADING_PREFIX = /^(?:\d+(?:\.\d+)*[.)]?|[IVXLCDM]+\.|[A-Z]\.)\s+\S/;

export function headingAlign(block: BlockData, pageWidth: number, sourceText = ""): "left" | "center" {
  if (block.type !== "title" || pageWidth <= 0) return "left";
  const { x0, x1 } = block.bbox;
  if (Math.abs((x0 + x1 - pageWidth) / 2) > 0.05 * pageWidth) return "left";
  const margin = Math.min(x0, pageWidth - x1);
  if (margin >= 0.14 * pageWidth) return "center";
  const plain = sourceText.replace(/<\/?(?:b|i|sup|sub)>/g, "").trim();
  return margin >= 0.06 * pageWidth && !SECTION_HEADING_PREFIX.test(plain) ? "center" : "left";
}

/**
 * 译文排版框：原文块的范围。解析器给单行文字的框只有字形高，装不下一整行时上下各补一半
 * （与后端 rendering/export.py 的 _box 一致）。
 */
export function layoutBox(block: BlockData, pageBlocks?: readonly BlockData[], pageWidth = 0, sourceText = "") {
  const { x0, y0, x1, y1 } = block.bbox;
  const pad = Math.max(block.font_size * LINE_HEIGHT + 0.5 - (y1 - y0), 0) / 2;
  const box = { x0, y0: y0 - pad, x1, y1: y1 + pad };
  if (!pageBlocks || pageWidth <= 0 || block.type !== "title" || x1 <= x0 || box.y1 <= box.y0) return box;
  if (headingAlign(block, pageWidth, sourceText) !== "left") return box;
  const mid = pageWidth / 2;
  const left = x1 <= mid + block.font_size;
  const right = x0 >= mid - block.font_size;
  const maxLeft = x0 + Math.max(24, 3 * block.font_size);
  const bodies = pageBlocks.filter((b) => b.type === "text" && b.page === block.page && b.id !== block.id);
  const hasLeftCol = bodies.some((b) => b.bbox.x0 <= maxLeft && b.bbox.x1 <= mid + b.font_size);
  const hasRightCol = bodies.some((b) => b.bbox.x0 >= mid - b.font_size);
  let colRight = x1;
  let safeRight = pageWidth - BOTTOM_MARGIN;
  for (const other of pageBlocks) {
    if (other.id === block.id || other.page !== block.page) continue;
    const ob = other.bbox;
    if (ob.y0 < box.y1 && ob.y1 > box.y0 && ob.x0 >= x1 - 2) {
      safeRight = Math.min(safeRight, ob.x0 - COLUMN_MIN_GAP);
    }
  }
  for (const other of bodies) {
    const ob = other.bbox;
    if (ob.x0 > maxLeft) continue;
    let sameColumn: boolean;
    if (left && hasLeftCol) sameColumn = ob.x1 <= mid + other.font_size;
    else if (right && hasRightCol) sameColumn = ob.x0 >= mid - other.font_size;
    else if ((left || right) && (hasLeftCol || hasRightCol)) continue;
    else sameColumn = ob.x1 >= x1 - 2;
    if (sameColumn) colRight = Math.max(colRight, ob.x1);
  }
  box.x1 = Math.max(x1, Math.min(colRight, safeRight));
  return box;
}

interface RenderGroup {
  anchor?: BlockData;
  members: Set<string>;
  bounds?: BlockData["bbox"];
  chars: number;
}

function mergedBounds(prev: BlockData, next: BlockData, group: RenderGroup, neighbors: readonly BlockData[]) {
  if (!group.bounds || prev.page !== next.page || prev.type !== "text" || next.type !== "text") return;
  const a = prev.bbox;
  const b = next.bbox;
  const size = Math.max(prev.font_size, next.font_size);
  const minWidth = Math.min(a.x1 - a.x0, b.x1 - b.x0);
  const gap = b.y0 - a.y1;
  if (
    minWidth <= 0 ||
    Math.min(a.y1 - a.y0, b.y1 - b.y0) <= 0 ||
    Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0) < 0.7 * minWidth ||
    Math.abs(a.x0 - b.x0) > 1.2 * size ||
    b.x1 > a.x1 + 1.5 * size ||
    gap < -0.5 * size ||
    gap > 1.5 * size
  ) return;
  const { x0, y0, x1, y1 } = group.bounds;
  const union = { x0: Math.min(x0, b.x0), y0: Math.min(y0, b.y0), x1: Math.max(x1, b.x1), y1: Math.max(y1, b.y1) };
  const blocked = neighbors.some(({ id, bbox }) =>
    id !== next.id && !group.members.has(id) &&
    Math.min(union.x1, bbox.x1) > Math.max(union.x0, bbox.x0) &&
    Math.min(union.y1, bbox.y1) > Math.max(union.y0, bbox.y0),
  );
  return blocked ? undefined : union;
}

function mergeRenderParts(seg: SegmentData, blocks: Map<string, BlockData>, byPage: Map<number, BlockData[]>) {
  const parts = seg.parts.length
    ? seg.parts
    : seg.block_ids.slice(0, 1).map((id) => ({ block_id: id, chars: seg.source.length }));
  const groups: RenderGroup[] = [];
  let prev: BlockData | undefined;
  for (const part of parts) {
    const block = blocks.get(part.block_id);
    const group = groups.at(-1);
    const bounds = prev && block && group && mergedBounds(prev, block, group, byPage.get(block.page) ?? []);
    if (group && bounds) {
      group.bounds = bounds;
      group.members.add(part.block_id);
      group.chars += part.chars;
    } else {
      groups.push({ anchor: block, members: new Set([part.block_id]), bounds: block?.bbox, chars: part.chars });
    }
    prev = block;
  }
  return groups;
}

export function blockTexts(doc: DocumentData): Map<string, BlockText> {
  const blocks = new Map(doc.pages.flatMap((p) => p.blocks).map((b) => [b.id, b]));
  const byPage = new Map(doc.pages.map((p) => [p.index, p.blocks]));
  const out = new Map<string, BlockText>();
  for (const seg of doc.segments) {
    const translated = Boolean(seg.target) && seg.status !== "failed";
    const text = translated ? (seg.target ?? "") : seg.source;
    const groups = mergeRenderParts(seg, blocks, byPage);
    const weights = groups.map((g) => g.chars);
    const pieces = splitByParts(text, weights);
    const sourcePieces = translated ? splitByParts(seg.source, weights) : pieces;
    groups.forEach(({ anchor: block, members, bounds }, i) => {
      if (block)
        out.set(block.id, {
          block,
          renderBox: members.size > 1 ? bounds : undefined,
          segment: seg,
          text: pieces[i] ?? "",
          sourceText: sourcePieces[i] ?? "",
          translated,
          first: i === 0,
        });
    });
  }
  return out;
}

export function progressOf(doc: DocumentData): { done: number; failed: number; total: number } {
  const total = doc.segments.length;
  const failed = doc.segments.filter((s) => s.status === "failed").length;
  const done = doc.segments.filter((s) => s.status === "done" || s.status === "edited").length;
  return { done, failed, total };
}

/** 用一个段落更新文档数据（编辑、重翻之后）。 */
export function withSegment(doc: DocumentData, seg: SegmentData): DocumentData {
  return { ...doc, segments: doc.segments.map((s) => (s.id === seg.id ? seg : s)) };
}
