import { describe, expect, it } from "vitest";

import {
  buildPageLayout,
  captureScrollAnchor,
  currentPageAt,
  hitBlockAt,
  PAGE_ROW_GAP,
  pageJumpTop,
  restoreScrollAnchor,
  visiblePageRange,
} from "./virtual";
import type { BlockData, PageData } from "@/shared/api/client";

function makePage(index: number, height: number, width = 600): PageData {
  return { index, width, height, kind: "native", missing_layout: false, blocks: [] };
}

function makeBlock(id: string, x0: number, y0: number, x1: number, y1: number): BlockData {
  return {
    id,
    page: 0,
    type: "text",
    bbox: { x0, y0, x1, y1 },
    font_size: 12,
    color: 0,
    bold: false,
    italic: false,
    serif: true,
    translatable: true,
    list_item: false,
  };
}

describe("阅读器虚拟化几何计算", () => {
  it("按精确页高和间距计算每页偏移与总高度", () => {
    const layout = buildPageLayout([makePage(0, 800), makePage(1, 1000), makePage(2, 600)], 0.5, PAGE_ROW_GAP);
    expect(layout.items).toEqual([
      { index: 0, order: 0, top: 0, height: 400, bottom: 400 },
      { index: 1, order: 1, top: 428, height: 500, bottom: 928 },
      { index: 2, order: 2, top: 956, height: 300, bottom: 1256 },
    ]);
    expect(layout.totalHeight).toBe(1256);
    expect(layout.byPageIndex.get(1)?.top).toBe(428);
  });

  it("空文档布局与异常缩放/负高度输入具备单调性护栏", () => {
    const empty = buildPageLayout([], 1);
    expect(empty.totalHeight).toBe(0);
    expect(visiblePageRange(empty, 0, 900, 1200)).toEqual([0, -1]);
    expect(currentPageAt(empty, 0)).toBe(0);

    const safe = buildPageLayout([makePage(0, -100), makePage(1, 800)], 0, 28);
    expect(safe.items[0]?.height).toBe(0);
    expect(safe.items[1]?.height).toBe(800);
    expect(safe.totalHeight).toBe(828);
  });

  it("支持非零起始或不连续的 page.index", () => {
    const layout = buildPageLayout([makePage(2, 800), makePage(5, 800)], 1, 28);
    expect(layout.byPageIndex.get(5)?.top).toBe(828);
    expect(currentPageAt(layout, 900, 80)).toBe(5);
  });

  it("100 页长文档仅返回视口与缓冲区内的少量页面", () => {
    const pages = Array.from({ length: 100 }, (_, i) => makePage(i, 800));
    const layout = buildPageLayout(pages, 1, 28); // 每页步进 828px
    expect(visiblePageRange(layout, 0, 900, 1200)).toEqual([0, 2]);
    const [start, end] = visiblePageRange(layout, 41400, 900, 1200);
    expect(start).toBe(48);
    expect(end).toBe(52);
    expect(visiblePageRange(layout, layout.totalHeight, 900, 1200)).toEqual([98, 99]);
  });

  it("按布局坐标与锚点偏移精确判定当前页", () => {
    const pages = Array.from({ length: 10 }, (_, i) => makePage(i, 800));
    const layout = buildPageLayout(pages, 1, 28);
    expect(currentPageAt(layout, 0, 80)).toBe(0);
    expect(currentPageAt(layout, 747, 80)).toBe(0);
    expect(currentPageAt(layout, 748, 80)).toBe(1);
    expect(currentPageAt(layout, 99999, 80)).toBe(9);
  });

  it("精确计算跳转纵坐标并支持实测缩放比例覆盖", () => {
    const pages = [makePage(0, 800), makePage(1, 800)];
    const layout = buildPageLayout(pages, 1, 28);
    expect(pageJumpTop(layout, 0, 100, 1, 56, 0)).toBe(44);
    expect(pageJumpTop(layout, 0, 10, 1, 56, 0)).toBe(0);
    expect(pageJumpTop(layout, 1, 100, 1, 56, 24)).toBe(24 + 828 + 100 - 56);
    expect(pageJumpTop(layout, 0, 100, 1, 56, 0, 1.5)).toBe(94);
    expect(pageJumpTop(layout, 99, 0, 1)).toBeNull();
  });

  it("缩放前后通过锚点精确保持阅读页与页内 PDF 偏移", () => {
    const pages = Array.from({ length: 50 }, (_, i) => makePage(i, 800));
    const oldLayout = buildPageLayout(pages, 1, 28);
    const newLayout = buildPageLayout(pages, 2, 28);
    // 位于第 25 页顶部往下 200pt 处
    const oldLayoutScroll = oldLayout.byPageIndex.get(25)!.top + 200;
    const anchor = captureScrollAnchor(oldLayout, oldLayoutScroll, 1);
    expect(anchor).toEqual({ pageIndex: 25, offsetInPt: 200 });

    const restoredScrollTop = restoreScrollAnchor(newLayout, anchor!, 2, 24);
    expect(restoredScrollTop).toBe(24 + newLayout.byPageIndex.get(25)!.top + 400);
    expect(currentPageAt(newLayout, restoredScrollTop! - 24, 0)).toBe(25);
  });

  it("坐标命中块时返回包含该点的最小正面积块，跳过零面积退化框", () => {
    const degenerate = makeBlock("zero", 60, 60, 60, 80);
    const outer = makeBlock("outer", 10, 10, 200, 200);
    const inner = makeBlock("inner", 50, 50, 100, 80);
    expect(hitBlockAt([degenerate, outer, inner], 60, 60)?.id).toBe("inner");
    expect(hitBlockAt([outer, inner], 20, 20)?.id).toBe("outer");
    expect(hitBlockAt([outer, inner], 300, 300)).toBeNull();
  });
});
