/**
 * 译文块排版拟合（与导出 PDF 的 rendering/export.py 一致）：按原字号依次尝试 1.5→1.2 倍行距，
 * 都放不下时用最紧的行距二分查找字号，下限为原字号的 70%。
 */
import { LINE_HEIGHTS } from "@/shared/text/typeset";

export const MIN_SCALE = 0.7;

export interface FitResult {
  fontSize: number;
  lineHeight: number;
  overflow: boolean;
}

/** fits(fontSize, lineHeight) 判断内容是否放得下（由调用方测量）。 */
export function searchFit(base: number, fits: (size: number, lh: number) => boolean): FitResult {
  for (const lh of LINE_HEIGHTS) {
    if (fits(base, lh)) return { fontSize: base, lineHeight: lh, overflow: false };
  }
  const lh = LINE_HEIGHTS[LINE_HEIGHTS.length - 1] ?? 1.05;
  let lo = base * MIN_SCALE;
  let hi = base;
  if (!fits(lo, lh)) return { fontSize: lo, lineHeight: lh, overflow: true };
  for (let i = 0; i < 8; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid, lh)) lo = mid;
    else hi = mid;
  }
  return { fontSize: Math.round(lo * 100) / 100, lineHeight: lh, overflow: false };
}

/** 在真实 DOM 上拟合：容器高度固定，测量内层内容高度。 */
export function fitElement(box: HTMLElement, inner: HTMLElement, base: number): FitResult {
  const height = box.clientHeight;
  const result = searchFit(base, (size, lh) => {
    box.style.fontSize = `${size}px`;
    box.style.lineHeight = String(lh);
    return inner.scrollHeight <= height + 0.5;
  });
  box.style.fontSize = `${result.fontSize}px`;
  box.style.lineHeight = String(result.lineHeight);
  return result;
}
