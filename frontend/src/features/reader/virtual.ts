/**
 * 阅读器虚拟化几何计算与原文块坐标命中（纯函数，无 DOM 副作用）。
 * 每页原始尺寸（pt）静态已知，按 scale 与固定行间距用前缀和精确算偏移，无需运行时测量 DOM。
 */
import type { BlockData, PageData } from "@/shared/api/client";

export const PAGE_ROW_GAP = 28; // 与 styles.css 中页面行间距一致
export const DEFAULT_OVERSCAN_PX = 1200; // 视口上下缓冲距离（约 1~2 页）

export interface PageMetric {
  index: number; // 数据里的 page.index
  order: number; // doc.pages 中的数组下标
  top: number; // 距离虚拟内容顶部的像素偏移
  height: number; // 缩放后的页面高度（px）
  bottom: number; // top + height
}

export interface PageLayout {
  items: PageMetric[];
  byPageIndex: Map<number, PageMetric>;
  totalHeight: number;
}

/** 根据每页尺寸与缩放比例构建精确纵向布局。 */
export function buildPageLayout(pages: readonly PageData[], scale: number, gap = PAGE_ROW_GAP): PageLayout {
  const items: PageMetric[] = [];
  const byPageIndex = new Map<number, PageMetric>();
  let cursor = 0;
  for (let order = 0; order < pages.length; order++) {
    const page = pages[order];
    if (!page) continue;
    const height = page.height * scale;
    const metric: PageMetric = {
      index: page.index,
      order,
      top: cursor,
      height,
      bottom: cursor + height,
    };
    items.push(metric);
    byPageIndex.set(page.index, metric);
    cursor = metric.bottom + (order < pages.length - 1 ? gap : 0);
  }
  return { items, byPageIndex, totalHeight: cursor };
}

/** 二分查找视口与缓冲区相交的页面顺序闭区间 [start, end]；空文档返回 [0, -1]。 */
export function visiblePageRange(
  layout: PageLayout,
  scrollTop: number,
  viewportHeight: number,
  overscan = DEFAULT_OVERSCAN_PX,
): [number, number] {
  const { items } = layout;
  const n = items.length;
  if (n === 0) return [0, -1];
  const minY = Math.max(0, scrollTop - overscan);
  const maxY = Math.max(minY, scrollTop + viewportHeight + overscan);

  // 第一个 bottom >= minY 的页
  let lo = 0;
  let hi = n - 1;
  let start = n - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    const item = items[mid];
    if (item && item.bottom >= minY) {
      start = mid;
      hi = mid - 1;
    } else {
      lo = mid + 1;
    }
  }

  // 最后一个 top <= maxY 的页
  lo = start;
  hi = n - 1;
  let end = start;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    const item = items[mid];
    if (item && item.top <= maxY) {
      end = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }

  return [start, end];
}

/** 二分查找当前阅读页（最后一个 top <= scrollTop + anchorOffset 的页），返回其 page.index。 */
export function currentPageAt(layout: PageLayout, scrollTop: number, anchorOffset = 80): number {
  const { items } = layout;
  if (items.length === 0) return 0;
  const target = scrollTop + anchorOffset;
  let lo = 0;
  let hi = items.length - 1;
  let found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    const item = items[mid];
    if (item && item.top <= target) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return items[found]?.index ?? 0;
}

/** 计算跳转到指定页、指定页内纵坐标（PDF pt）时的滚动位置（px）。 */
export function pageJumpTop(
  layout: PageLayout,
  pageIndex: number,
  topInPt: number,
  scale: number,
  inset = 56,
  paddingTop = 0,
  actualScale?: number,
): number | null {
  const metric = layout.byPageIndex.get(pageIndex);
  if (!metric) return null;
  const effectiveScale = actualScale && actualScale > 0 ? actualScale : scale;
  return Math.max(paddingTop + metric.top + topInPt * effectiveScale - inset, 0);
}

/** 根据页内 PDF 点坐标 (xPt, yPt) 命中包含该点的最小面积块（用于原文页悬停与点击联动）。 */
export function hitBlockAt(blocks: readonly BlockData[], xPt: number, yPt: number): BlockData | null {
  let best: BlockData | null = null;
  let bestArea = Infinity;
  for (const block of blocks) {
    const { x0, y0, x1, y1 } = block.bbox;
    if (xPt < x0 || xPt > x1 || yPt < y0 || yPt > y1) continue;
    const area = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
    if (area < bestArea) {
      bestArea = area;
      best = block;
    }
  }
  return best;
}
