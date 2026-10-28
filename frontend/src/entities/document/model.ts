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
      if (block) out.set(id, { block, segment: seg, text: pieces[i] ?? "", translated });
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
