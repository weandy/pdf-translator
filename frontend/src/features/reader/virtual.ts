/**
 * 阅读器虚拟化几何计算、缩放锚点保持与原文块坐标命中（纯函数，无 DOM 副作用）。
 * 所有布局纵坐标均以虚拟内容顶部（不含外层 .pages 的 paddingTop）为 0 点，避免坐标系混用。
 */
import type { BlockData, PageData } from "@/shared/api/client";

export const PAGE_ROW_GAP = 28; // 页面行间距（px），与虚拟化布局一致
export const DEFAULT_OVERSCAN_PX = 1200; // 视口上下缓冲距离（约 1~2 页）
export const CURRENT_PAGE_ANCHOR_PX = 80; // 判定当前页的视口顶部锚点偏移（px）

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

export interface ScrollAnchor {
  pageIndex: number;
  offsetInPt: number; // 锚点距该页顶部的 PDF 点偏移
}

/** 根据每页尺寸与缩放比例构建精确纵向布局。 */
export function buildPageLayout(pages: readonly PageData[], scale: number, gap = PAGE_ROW_GAP): PageLayout {
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1;
  const safeGap = Number.isFinite(gap) && gap >= 0 ? gap : 0;
  const items: PageMetric[] = [];
  const byPageIndex = new Map<number, PageMetric>();
  let cursor = 0;
  for (let order = 0; order < pages.length; order++) {
    const page = pages[order];
    if (!page) continue;
    if (items.length > 0) cursor += safeGap;
    const height = Math.max(0, page.height * safeScale);
    const metric: PageMetric = {
      index: page.index,
      order,
      top: cursor,
      height,
      bottom: cursor + height,
    };
    items.push(metric);
    byPageIndex.set(page.index, metric);
    cursor = metric.bottom;
  }
  return { items, byPageIndex, totalHeight: cursor };
}

/** 二分查找视口与缓冲区相交的页面顺序闭区间 [start, end]（输入为布局坐标）；空文档返回 [0, -1]。 */
export function visiblePageRange(
  layout: PageLayout,
  layoutScrollTop: number,
  viewportHeight: number,
  overscan = DEFAULT_OVERSCAN_PX,
): [number, number] {
  const { items } = layout;
  const n = items.length;
  if (n === 0) return [0, -1];
  const safeScroll = Math.max(0, layoutScrollTop);
  const safeViewport = Math.max(0, viewportHeight);
  const safeOverscan = Math.max(0, overscan);
  const minY = Math.max(0, safeScroll - safeOverscan);
  const maxY = Math.max(minY, safeScroll + safeViewport + safeOverscan);

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

/** 二分查找当前阅读页（最后一个 top <= layoutScrollTop + anchorOffset 的页），返回其 page.index。 */
export function currentPageAt(
  layout: PageLayout,
  layoutScrollTop: number,
  anchorOffset = CURRENT_PAGE_ANCHOR_PX,
): number {
  const { items } = layout;
  if (items.length === 0) return 0;
  const target = Math.max(0, layoutScrollTop) + anchorOffset;
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

/** 计算跳转到指定页、指定页内纵坐标（PDF pt）时的容器滚动位置 scrollTop（px）。 */
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

/** 捕获缩放/切换布局前的阅读锚点（锚定在视口顶部，精确记录所在页及页内 PDF 点偏移）。 */
export function captureScrollAnchor(layout: PageLayout, layoutScrollTop: number, scale: number): ScrollAnchor | null {
  if (layout.items.length === 0 || scale <= 0) return null;
  const clamped = Math.max(0, layoutScrollTop);
  const pageIndex = currentPageAt(layout, clamped, 0);
  const metric = layout.byPageIndex.get(pageIndex);
  if (!metric) return null;
  const offsetInPx = Math.max(0, clamped - metric.top);
  return { pageIndex, offsetInPt: offsetInPx / scale };
}

/** 将阅读锚点还原到新缩放布局下的容器滚动位置 scrollTop（px）。 */
export function restoreScrollAnchor(
  layout: PageLayout,
  anchor: ScrollAnchor,
  scale: number,
  paddingTop = 0,
): number | null {
  const metric = layout.byPageIndex.get(anchor.pageIndex);
  if (!metric || scale <= 0) return null;
  return Math.max(0, paddingTop + metric.top + anchor.offsetInPt * scale);
}

/** 根据页内 PDF 点坐标 (xPt, yPt) 命中包含该点的最小正面积块（跳过退化零面积框）。 */
export function hitBlockAt(blocks: readonly BlockData[], xPt: number, yPt: number): BlockData | null {
  let best: BlockData | null = null;
  let bestArea = Infinity;
  for (const block of blocks) {
    const { x0, y0, x1, y1 } = block.bbox;
    if (x1 <= x0 || y1 <= y0) continue;
    if (xPt < x0 || xPt > x1 || yPt < y0 || yPt > y1) continue;
    const area = (x1 - x0) * (y1 - y0);
    if (area < bestArea) {
      bestArea = area;
      best = block;
    }
  }
  return best;
}
