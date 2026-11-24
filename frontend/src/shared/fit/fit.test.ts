import { describe, expect, it } from "vitest";

import type { BlockData, DocumentData, SegmentData } from "@/shared/api/client";
import { MIN_SCALE, searchFit } from "@/shared/fit/fit";

// 模拟测量：内容高度 = 字号 × 行距 × 行数，行数随字号线性增加
const model = (chars: number, width: number, height: number) => (size: number, lh: number) => {
  const lines = Math.ceil((chars * size) / width);
  return lines * size * lh <= height;
};

describe("searchFit", () => {
  it("放得下时保持原字号，优先用宽松行距", () => {
    expect(searchFit(10, model(20, 400, 100))).toEqual({ fontSize: 10, lineHeight: 1.5, overflow: false });
  });

  it("先收紧行距，再缩字号", () => {
    const r = searchFit(10, model(80, 400, 26));
    expect(r.fontSize).toBe(10);
    expect(r.lineHeight).toBeLessThan(1.5);
  });

  it("再缩字号，不低于下限", () => {
    const r = searchFit(10, model(400, 400, 80));
    expect(r.overflow).toBe(false);
    expect(r.fontSize).toBeLessThan(10);
    expect(r.fontSize).toBeGreaterThanOrEqual(10 * MIN_SCALE);
  });

  it("触底仍放不下时标记溢出", () => {
    expect(searchFit(10, () => false)).toMatchObject({ fontSize: 7, overflow: true });
  });
});

const block = (id: string, type: string, x0: number, y0: number, x1: number, y1: number): BlockData => ({
  id, page: 0, type, bbox: { x0, y0, x1, y1 }, font_size: 7.97, bold: false, italic: false,
  serif: true, color: 0, translatable: type !== "figure", list_item: false,
});

describe("阅读器排版框与字号拟合", () => {
  const title = block("title", "title", 36, 100, 122, 110);
  const body = block("body", "text", 36, 112, 286, 180);
  const abstract = block("abstract", "text", 36, 35, 560, 70);
  const right = block("right", "text", 314, 110, 564, 150);

  it("左栏短标题扩至同栏正文宽，17 字保持 7.97pt", async () => {
    const { layoutBox } = await import("@/entities/document/model");
    const base = layoutBox(title);
    const box = layoutBox(title, [title, abstract, right, body], 600);
    expect(base.x1 - base.x0).toBe(86);
    expect(box.x1 - box.x0).toBe(250);
    expect(searchFit(title.font_size, model(17, box.x1 - box.x0, box.y1 - box.y0)).fontSize).toBe(7.97);
    expect(layoutBox(title, [abstract, right], 600)).toEqual(base);
  });

  it("右栏取本栏右缘，同高图块前保留 8pt", async () => {
    const { layoutBox } = await import("@/entities/document/model");
    const figure = block("figure", "figure", 160, 101, 280, 150);
    expect(layoutBox(title, [body, figure, abstract], 600).x1).toBe(152);
    const rightTitle = block("heading", "title", 314, 90, 400, 100);
    expect(layoutBox(rightTitle, [body, right, abstract], 600).x1).toBe(564);
  });

  it("单栏的 86pt 短标题扩至 455pt 正文宽，保持原字号", async () => {
    const { layoutBox } = await import("@/entities/document/model");
    const heading = block("heading", "title", 70, 100, 156, 110);
    const body = block("body", "text", 70, 120, 525, 180);
    const box = layoutBox(heading, [heading, body], 595);
    expect(box.x1 - box.x0).toBe(455);
    expect(searchFit(heading.font_size, model(17, box.x1 - box.x0, box.y1 - box.y0))).toMatchObject({
      fontSize: heading.font_size, overflow: false,
    });
  });

  it("对称主标题居中，通栏编号小节按原文左对齐", async () => {
    const { headingAlign, layoutBox } = await import("@/entities/document/model");
    const centered = block("center", "title", 150, 100, 450, 110);
    expect(headingAlign(centered, 600)).toBe("center");
    expect(layoutBox(centered, [abstract], 600)).toEqual(layoutBox(centered));
    const wide = block("wide", "title", 50, 100, 550, 110);
    expect(headingAlign(wide, 600, "<b>2.1</b> Synthetic")).toBe("left");
    expect(layoutBox(wide, [abstract], 600, "<b>2.1</b> Synthetic").x1).toBe(560);
  });

  it("同栏续段共用一个外接框，夹图或跨栏时按原块显示", async () => {
    const { blockTexts } = await import("@/entities/document/model");
    const a = block("a", "text", 36, 40, 235, 64);
    const b = block("b", "text", 40, 68, 228, 90);
    const seg: SegmentData = {
      id: "s", block_ids: ["a", "b"], parts: [{ block_id: "a", chars: 8 }, { block_id: "b", chars: 8 }],
      source: "Synthetic source text", target: "同栏连接，function结束。", formulas: {},
      status: "done", quality: "default", translation_attempts: 0, failure_reason: "",
    };
    const texts = (blocks: BlockData[]) => blockTexts({
      title: "", job: {} as DocumentData["job"], segments: [seg], outline: [], links: [],
      pages: [{ index: 0, width: 600, height: 300, kind: "scanned", missing_layout: false, blocks }],
    });
    const merged = texts([a, b]);
    expect([...merged.keys()]).toEqual(["a"]);
    expect(merged.get("a")).toMatchObject({
      renderBox: { x0: 36, y0: 40, x1: 235, y1: 90 }, text: seg.target, sourceText: seg.source, first: true,
    });
    for (const blocks of [[a, b, block("figure", "figure", 50, 65, 80, 67)], [a, { ...right, id: "b" }]]) {
      const parts = [...texts(blocks).values()];
      expect(parts).toHaveLength(2);
      expect(parts.every((p) => p.renderBox === undefined && p.text.length > 0)).toBe(true);
      expect(parts.map((p) => p.text).join("")).toBe(seg.target);
    }
  });
});
