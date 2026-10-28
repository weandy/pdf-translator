import { describe, expect, it } from "vitest";

import { MIN_SCALE, searchFit } from "@/shared/fit/fit";

// 模拟测量：内容高度 = 字号 × 行距 × 行数，行数随字号线性增加
const model = (chars: number, width: number, height: number) => (size: number, lh: number) => {
  const lines = Math.ceil((chars * size) / width);
  return lines * size * lh <= height;
};

describe("searchFit", () => {
  it("放得下时保持原字号，行距与导出 PDF 一致", () => {
    expect(searchFit(10, model(20, 400, 100))).toEqual({ fontSize: 10, lineHeight: 1.2, overflow: false });
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
