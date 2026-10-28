/**
 * 阅读器数据的派生结构：每个块要显示的文字（跨栏/跨页段落的译文按各块原文长度拆开）。
 */
import type { BlockData, DocumentData, SegmentData } from "@/shared/api/client";
import { splitByParts } from "@/shared/text/split";

export interface BlockText {
  block: BlockData;
  segment: SegmentData;
  text: string; // 译文片段；没有译文（失败/未完成）时为原文片段
  translated: boolean;
  first: boolean; // 段落的第一部分（跨栏/跨页段落拆开后）
}

/** 排版角色决定字体（GB/T 7713.2 附录 B），与后端 rendering/export.py 的 role() 一致。 */
export type Role = "heading" | "caption" | "body";

export function roleOf(block: BlockData): Role {
  if (block.type === "title" || block.bold) return "heading";
  if (block.type === "caption") return "caption";
  return "body";
}

/** 与导出 PDF 一致的行距（rendering/export.py 的 LINE_HEIGHT）。 */
export const LINE_HEIGHT = 1.2;

/**
 * 译文排版框：原文块的范围。解析器给单行文字的框只有字形高，装不下一整行时上下各补一半
 * （与后端 rendering/export.py 的 _box 一致）。
 */
export function layoutBox(block: BlockData): { x0: number; y0: number; x1: number; y1: number } {
  const { x0, y0, x1, y1 } = block.bbox;
  const pad = Math.max(block.font_size * LINE_HEIGHT + 0.5 - (y1 - y0), 0) / 2;
  return { x0, y0: y0 - pad, x1, y1: y1 + pad };
}

export function blockTexts(doc: DocumentData): Map<string, BlockText> {
  const blocks = new Map(doc.pages.flatMap((p) => p.blocks).map((b) => [b.id, b]));
  const out = new Map<string, BlockText>();
  for (const seg of doc.segments) {
    const translated = Boolean(seg.target) && seg.status !== "failed";
    const text = translated ? (seg.target ?? "") : seg.source;
    const pieces = splitByParts(
      text,
      seg.parts.map((p) => p.chars),
    );
    seg.block_ids.forEach((id, i) => {
      const block = blocks.get(id);
      if (block) out.set(id, { block, segment: seg, text: pieces[i] ?? "", translated, first: i === 0 });
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
