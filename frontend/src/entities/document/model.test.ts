import { describe, expect, it } from "vitest";

import { blockTexts, progressOf } from "@/entities/document/model";
import type { DocumentData, SegmentData } from "@/shared/api/client";

const block = (id: string, page: number) => ({
  id, page, type: "text", bbox: { x0: 0, y0: 0, x1: 100, y1: 20 }, font_size: 10, bold: false, italic: false,
  serif: true, color: 0, translatable: true,
});  // prettier-ignore

const seg = (over: Partial<SegmentData>): SegmentData => ({
  id: "s", block_ids: ["p0-b0"], parts: [{ block_id: "p0-b0", chars: 10 }], source: "src", target: "译文",
  status: "done", quality: "default", formulas: {}, ...over,
});  // prettier-ignore

const doc = (segments: SegmentData[]): DocumentData => ({
  job: {} as DocumentData["job"],
  title: "T",
  pages: [
    { index: 0, width: 600, height: 800, kind: "native", blocks: [block("p0-b0", 0), block("p0-b1", 0)] },
    { index: 1, width: 600, height: 800, kind: "native", blocks: [block("p1-b0", 1)] },
  ],
  segments,
});

describe("blockTexts", () => {
  it("跨页段落的译文拆到各块", () => {
    const s = seg({
      id: "a", block_ids: ["p0-b1", "p1-b0"],
      parts: [{ block_id: "p0-b1", chars: 50 }, { block_id: "p1-b0", chars: 50 }],
      target: "前半段的译文内容，后半段的译文内容。",
    });  // prettier-ignore
    const map = blockTexts(doc([s]));
    const a = map.get("p0-b1")?.text ?? "";
    const b = map.get("p1-b0")?.text ?? "";
    expect(a + b).toBe(s.target);
    expect(a.length).toBeGreaterThan(0);
    expect(b.length).toBeGreaterThan(0);
  });

  it("失败的段落显示原文并标记未翻译", () => {
    const map = blockTexts(doc([seg({ status: "failed", target: null, source: "Original" })]));
    expect(map.get("p0-b0")).toMatchObject({ text: "Original", translated: false });
  });

  it("统计进度", () => {
    expect(progressOf(doc([seg({}), seg({ id: "b", status: "failed" }), seg({ id: "c", status: "edited" })]))).toEqual({
      done: 2,
      failed: 1,
      total: 3,
    });
  });
});
