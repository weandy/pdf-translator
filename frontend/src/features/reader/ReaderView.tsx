/**
 * 阅读器（docs/03-mvp-design.md §4.2，docs/08-reader-virtualization-and-text-layer-design.md）：
 * 每一行是同一页的原文与译文，共用一个滚动条，天然对齐。
 * 长文档采用基于精确页高的零依赖窗口虚拟化，仅挂载视口及上下缓冲区内的页面。
 */
import type { PDFDocumentProxy } from "pdfjs-dist/legacy/build/pdf.mjs";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import "./fonts.css";

import { blockTexts, layoutBox, roleOf, type BlockText } from "@/entities/document/model";
import {
  fileUrl,
  formulaUrl,
  type BlockData,
  type DocumentData,
  type LinkData,
  type OutlineData,
  type PageData,
} from "@/shared/api/client";
import { readJSON, writeJSON } from "@/shared/storage/local";
import { fitElement } from "@/shared/fit/fit";
import { InlineMathText, type InlineLink } from "@/shared/math/inline";
import { countCompact } from "@/shared/text/links";
import { splitCaptionNo } from "@/shared/text/script";
import { dropFakeScripts, keyActivate, shortLabel, spaceCjkLatin, wantsIndent } from "@/shared/text/typeset";
import { PdfCanvas, PdfTextLayer, usePdf } from "@/shared/pdf/pdf";

import { OutlinePanel } from "./OutlinePanel";
import { buildSearchIndex, querySearchIndex, type SearchHit } from "./search";
import {
  buildPageLayout,
  captureScrollAnchor,
  CURRENT_PAGE_ANCHOR_PX,
  currentPageAt,
  DEFAULT_OVERSCAN_PX,
  hitBlockAt,
  PAGE_ROW_GAP,
  pageJumpTop,
  restoreScrollAnchor,
  visiblePageRange,
  type ScrollAnchor,
} from "./virtual";

export type ViewMode = "both" | "translated" | "original";

interface Props {
  doc: DocumentData;
  jobId: string;
  selectedSegment: string | null;
  onSelectSegment: (id: string) => void;
  /** 工具栏左侧（标题等）与右侧（下载等），由页面组合 */
  heading?: ReactNode;
  actions?: ReactNode;
  /** 对照模式下译文在左边还是右边 */
  side?: "left" | "right";
  /** 每次变化时，若选中的段落不在视野里就滚过去（从修改记录打开段落时用） */
  revealKey?: number;
  /** 定位到术语所在词；n 变化时即使是同一段也再跳一次。 */
  spot?: { id: string; n: number; term?: { source: string; target: string } } | null;
  /** 淡淡标出的段落（术语面板里当前术语出现的段落） */
  marked?: ReadonlySet<string>;
}

const PADDING = 48; // .pages 左右内边距之和
const SPINE = 2; // 对照模式两页之间的装订线
const NARROW = 760; // 视口窄于这个宽度时默认只显示译文
const POSITIONS = "pdftr.pos.v1"; // 每篇文档上次读到的页（浏览器本地）
const SEARCH_DEBOUNCE_MS = 150;
const DEFAULT_VIEWPORT_HEIGHT = 800;
const DRAG_THRESHOLD_PX = 5;

interface JumpTarget {
  page: number;
  top: number;
}

function savedPage(jobId: string): number {
  return readJSON<Record<string, number>>(POSITIONS, {})[jobId] ?? 0;
}

function savePage(jobId: string, page: number): void {
  const all = readJSON<Record<string, number>>(POSITIONS, {});
  all[jobId] = page;
  const keys = Object.keys(all);
  if (keys.length > 100) for (const k of keys.slice(0, keys.length - 100)) delete all[k]; // 只记最近 100 篇
  writeJSON(POSITIONS, all);
}

function scrollerPaddingTop(el: HTMLElement | null): number {
  if (!el) return 0;
  return Number.parseFloat(window.getComputedStyle(el).paddingTop) || 0;
}

function isImeComposing(e: React.KeyboardEvent): boolean {
  return e.nativeEvent.isComposing || e.keyCode === 229;
}

export function ReaderView({
  doc,
  jobId,
  selectedSegment,
  onSelectSegment,
  heading,
  actions,
  side = "right",
  revealKey = 0,
  spot = null,
  marked,
}: Props) {
  const firstPageIndex = doc.pages[0]?.index ?? 0;
  // 窄屏（手机）上默认只显示译文、适应宽度；用户切换后以用户的选择为准
  const [mode, setMode] = useState<ViewMode>(() => (window.innerWidth < NARROW ? "translated" : "both"));
  const [zoom, setZoom] = useState<number | null>(null); // null = 自适应宽度
  const [width, setWidth] = useState(1200);
  const [viewportHeight, setViewportHeight] = useState(DEFAULT_VIEWPORT_HEIGHT);
  // 缩放无关的阅读锚点：scale / mode / 宽度变化时首帧即能挂载正确页，不跳页、不闪白
  const [windowAnchor, setWindowAnchor] = useState<ScrollAnchor>(() => ({
    pageIndex: firstPageIndex,
    offsetInPt: 0,
  }));
  // 短距离平滑滚动期间临时保留目标页在挂载集合中，避免动画首帧卸载起点页闪白
  const [pinnedPage, setPinnedPage] = useState<number | null>(null);
  const [failedTextPages, setFailedTextPages] = useState<ReadonlySet<number>>(() => new Set());
  const [hovered, setHovered] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null); // 刚跳到的标题段落：原文与译文上短暂高亮
  const flashTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const jumpTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const pinTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [jumpTarget, setJumpTarget] = useState<JumpTarget | null>(null);
  const [showOutline, setShowOutline] = useState(() => window.innerWidth >= 1280 && doc.outline.length > 0);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [searchIndex, setSearchIndex] = useState(0);
  const [current, setCurrent] = useState(firstPageIndex); // 当前页（page.index）
  const [pageDraft, setPageDraft] = useState<string | null>(null); // 正在输入的显示页码（1 起）
  const scroller = useRef<HTMLDivElement>(null);
  // 统一存布局坐标（已扣掉 paddingTop）
  const layoutScrollRef = useRef(0);
  const anchorRef = useRef<ScrollAnchor>({ pageIndex: firstPageIndex, offsetInPt: 0 });
  const pendingSavePageRef = useRef<number | null>(null);
  const frame = useRef(0);
  const syncFrame = useRef(0);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const restoreTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const searchTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const restored = useRef(false);

  // 只加载当前显示方式用得到的 PDF
  const original = usePdf(mode === "both" || mode === "original" ? fileUrl(jobId, "original.pdf") : null);
  const background = usePdf(mode === "both" || mode === "translated" ? fileUrl(jobId, "background.pdf") : null);
  const texts = useMemo(() => blockTexts(doc), [doc]);
  // 链接：原文页上全部按原位置；译文页上，译文块外的按原位置，块内的落在译文文字上
  const { byPage, byBlock } = useMemo(() => groupLinks(doc.links), [doc.links]);
  const fontsVersion = useFontsVersion();

  // 按文档预构建结构索引，避免交互和滚动时反复全量遍历
  const { blockById, blocksByPage, pagesBySegment, firstPageBySegment } = useMemo(() => {
    const bById = new Map<string, BlockData>();
    const bByPage = new Map<number, BlockData[]>();
    const pBySeg = new Map<string, Set<number>>();
    const firstBySeg = new Map<string, number>();
    for (const page of doc.pages) {
      const pageBlocks: BlockData[] = [];
      for (const b of page.blocks) {
        bById.set(b.id, b);
        const bt = texts.get(b.id);
        if (bt) {
          pageBlocks.push(b);
          const segId = bt.segment.id;
          let set = pBySeg.get(segId);
          if (!set) {
            set = new Set();
            pBySeg.set(segId, set);
          }
          set.add(page.index);
        }
      }
      bByPage.set(page.index, pageBlocks);
    }
    for (const seg of doc.segments) {
      const firstBlock = bById.get(seg.block_ids[0] ?? "");
      if (firstBlock) firstBySeg.set(seg.id, firstBlock.page);
    }
    return { blockById: bById, blocksByPage: bByPage, pagesBySegment: pBySeg, firstPageBySegment: firstBySeg };
  }, [doc, texts]);

  const maxPageWidth = useMemo(
    () => (doc.pages.length ? Math.max(...doc.pages.map((p) => p.width)) : 600),
    [doc.pages],
  );
  const columns = mode === "both" ? 2 : 1;
  const fitZoom = Math.max(0.3, (width - PADDING - (columns - 1) * SPINE) / columns / maxPageWidth);
  const scale = zoom ?? fitZoom;
  const layout = useMemo(() => buildPageLayout(doc.pages, scale, PAGE_ROW_GAP), [doc.pages, scale]);
  const contentMinWidth = maxPageWidth * scale * columns + (columns - 1) * SPINE;

  const windowLayoutScroll = useMemo(
    () => restoreScrollAnchor(layout, windowAnchor, scale, 0) ?? 0,
    [layout, windowAnchor, scale],
  );
  const [visibleStart, visibleEnd] = useMemo(
    () => visiblePageRange(layout, windowLayoutScroll, viewportHeight, DEFAULT_OVERSCAN_PX),
    [layout, windowLayoutScroll, viewportHeight],
  );
  const visibleRangeRef = useRef<[number, number]>([visibleStart, visibleEnd]);
  const layoutRef = useRef(layout);
  const scaleRef = useRef(scale);
  const viewportHeightRef = useRef(viewportHeight);
  const prevScaleRef = useRef(scale);

  useEffect(() => {
    visibleRangeRef.current = [visibleStart, visibleEnd];
    layoutRef.current = layout;
    scaleRef.current = scale;
    viewportHeightRef.current = viewportHeight;
  }, [visibleStart, visibleEnd, layout, scale, viewportHeight]);

  // 缩放或切换显示方式导致 scale 变化时，在绘制前把真实 scrollTop 同步回锚点对应的文档位置
  useLayoutEffect(() => {
    if (prevScaleRef.current === scale) return;
    prevScaleRef.current = scale;
    const el = scroller.current;
    if (!el) return;
    const paddingTop = scrollerPaddingTop(el);
    const restoredScrollTop = restoreScrollAnchor(layout, anchorRef.current, scale, paddingTop);
    if (restoredScrollTop === null) return;
    el.scrollTop = restoredScrollTop;
    const actualLayoutScroll = Math.max(0, el.scrollTop - paddingTop);
    layoutScrollRef.current = actualLayoutScroll;
    const clampedAnchor = captureScrollAnchor(layout, actualLayoutScroll, scale);
    if (clampedAnchor) anchorRef.current = clampedAnchor;
    if (syncFrame.current) cancelAnimationFrame(syncFrame.current);
    syncFrame.current = requestAnimationFrame(() => {
      syncFrame.current = 0;
      const synced = captureScrollAnchor(layoutRef.current, layoutScrollRef.current, scaleRef.current);
      if (synced) setWindowAnchor(synced);
      setCurrent(currentPageAt(layoutRef.current, layoutScrollRef.current, CURRENT_PAGE_ANCHOR_PX));
    });
  }, [layout, scale]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const nextWidth = entry.contentRect.width;
      const nextHeight = entry.contentRect.height;
      if (nextWidth > 0) {
        setWindowAnchor(anchorRef.current);
        setWidth(nextWidth);
      }
      if (nextHeight > 0) setViewportHeight(nextHeight);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 离开页面或卸载时立即落盘尚未写入的阅读页位置并清理所有定时器
  useEffect(() => {
    const flushPage = () => {
      if (pendingSavePageRef.current !== null) {
        savePage(jobId, pendingSavePageRef.current);
        pendingSavePageRef.current = null;
      }
    };
    window.addEventListener("pagehide", flushPage);
    return () => {
      window.removeEventListener("pagehide", flushPage);
      flushPage();
      clearTimeout(flashTimer.current);
      clearTimeout(jumpTimer.current);
      clearTimeout(pinTimer.current);
      clearTimeout(saveTimer.current);
      clearTimeout(restoreTimer.current);
      clearTimeout(searchTimer.current);
      if (frame.current) cancelAnimationFrame(frame.current);
      if (syncFrame.current) cancelAnimationFrame(syncFrame.current);
    };
  }, [jobId]);

  const searchIndexData = useMemo(() => buildSearchIndex(doc), [doc]);
  const searchResults = useMemo(
    () => querySearchIndex(searchIndexData, debouncedQuery),
    [searchIndexData, debouncedQuery],
  );
  const activeSearchIndex = Math.min(searchIndex, Math.max(0, searchResults.length - 1));
  const currentSearchId = searchResults[activeSearchIndex]?.segmentId ?? null;

  // 按页分组搜索命中与术语标记，确保无关页的 props 引用不变
  const searchIdsByPage = useMemo(() => {
    const map = new Map<number, Set<string>>();
    for (const hit of searchResults) {
      const pages = pagesBySegment.get(hit.segmentId);
      if (!pages) continue;
      for (const pageIdx of pages) {
        let set = map.get(pageIdx);
        if (!set) {
          set = new Set();
          map.set(pageIdx, set);
        }
        set.add(hit.segmentId);
      }
    }
    return map;
  }, [searchResults, pagesBySegment]);

  const markedByPage = useMemo(() => {
    const map = new Map<number, Set<string>>();
    if (!marked || marked.size === 0) return map;
    for (const segId of marked) {
      const pages = pagesBySegment.get(segId);
      if (!pages) continue;
      for (const pageIdx of pages) {
        let set = map.get(pageIdx);
        if (!set) {
          set = new Set();
          map.set(pageIdx, set);
        }
        set.add(segId);
      }
    }
    return map;
  }, [marked, pagesBySegment]);

  useEffect(() => {
    if (!searchOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSearchOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [searchOpen]);

  const handleTextLayerError = useCallback((pageIndex: number) => {
    setFailedTextPages((prev) => {
      if (prev.has(pageIndex)) return prev;
      const next = new Set(prev);
      next.add(pageIndex);
      return next;
    });
  }, []);

  // 跳到某页的某个位置（top 为离页顶的 PDF 点），不依赖目标页已挂载
  const goTo = useCallback(
    (page: number, top = 0, smooth = true, inset = 56) => {
      const el = scroller.current;
      if (!el) return;
      const paddingTop = scrollerPaddingTop(el);
      const pageWidth = doc.pages.find((p) => p.index === page)?.width;
      const shownWidth = el
        .querySelector<HTMLElement>(`.page-row[data-page="${page}"] .page`)
        ?.getBoundingClientRect().width;
      // 滚动条、侧栏会改变实际页宽；布局已更新但 ResizeObserver 中的比例可能还未同步。
      const actualScale = pageWidth && shownWidth ? shownWidth / pageWidth : undefined;
      const targetScrollTop = pageJumpTop(layout, page, top, scale, inset, paddingTop, actualScale);
      if (targetScrollTop === null) return;
      const currentScrollTop = paddingTop + layoutScrollRef.current;
      const distance = Math.abs(targetScrollTop - currentScrollTop);
      // 远距离跳转不做跨几十页的长平滑动画，直接落位避免闪白与无谓挂载
      const useSmooth = smooth && distance <= viewportHeightRef.current * 1.5;
      el.scrollTo({ top: targetScrollTop, behavior: useSmooth ? "smooth" : "auto" });

      const targetLayoutScroll = Math.max(0, targetScrollTop - paddingTop);
      const targetAnchor = captureScrollAnchor(layout, targetLayoutScroll, scale);
      if (useSmooth) {
        // 短距离平滑滚动：保留起点页不闪白，同时把目标页临时并入挂载集合
        setPinnedPage(page);
        clearTimeout(pinTimer.current);
        pinTimer.current = setTimeout(() => setPinnedPage(null), 900);
      } else {
        layoutScrollRef.current = targetLayoutScroll;
        if (targetAnchor) {
          anchorRef.current = targetAnchor;
          setWindowAnchor(targetAnchor);
        }
        setPinnedPage(null);
        setCurrent(page);
      }
      pendingSavePageRef.current = page;
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        savePage(jobId, page);
        pendingSavePageRef.current = null;
      }, 500);
    },
    [layout, scale, doc.pages, jobId],
  );

  const jump = useCallback(
    (page: number, top: number) => {
      goTo(page, top);
      setJumpTarget({ page, top });
      clearTimeout(jumpTimer.current);
      jumpTimer.current = setTimeout(() => setJumpTarget(null), 2400);
    },
    [goTo],
  );

  const jumpSearchHit = useCallback(
    (hit: SearchHit, index: number) => {
      const segment = doc.segments.find((s) => s.id === hit.segmentId);
      const block = blockById.get(segment?.block_ids[0] ?? "");
      if (!block) return;
      setSearchIndex(index);
      jump(block.page, block.bbox.y0);
      setFlash(hit.segmentId);
      clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(null), 2400);
    },
    [doc.segments, blockById, jump],
  );

  const onSearchChange = (value: string) => {
    setSearchQuery(value);
    setSearchIndex(0);
    clearTimeout(searchTimer.current);
    if (!value.trim()) {
      setDebouncedQuery("");
      return;
    }
    searchTimer.current = setTimeout(() => setDebouncedQuery(value), SEARCH_DEBOUNCE_MS);
  };

  const moveSearch = (delta: number, overrideHits?: SearchHit[]) => {
    const list = overrideHits ?? searchResults;
    if (!list.length) return;
    const base = overrideHits ? (delta > 0 ? -1 : 0) : activeSearchIndex;
    const next = (base + delta + list.length) % list.length;
    const hit = list[next];
    if (hit) jumpSearchHit(hit, next);
  };

  // 滚动时统一转成布局坐标（扣除 paddingTop），仅在窗口范围或当前页变化时更新 React 状态
  const onScroll = () => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      const el = scroller.current;
      if (!el) return;
      const paddingTop = scrollerPaddingTop(el);
      const layoutScroll = Math.max(0, el.scrollTop - paddingTop);
      layoutScrollRef.current = layoutScroll;
      const nextAnchor = captureScrollAnchor(layout, layoutScroll, scale);
      if (nextAnchor) anchorRef.current = nextAnchor;

      const [prevStart, prevEnd] = visibleRangeRef.current;
      const [nextStart, nextEnd] = visiblePageRange(layout, layoutScroll, viewportHeight, DEFAULT_OVERSCAN_PX);
      if ((nextStart !== prevStart || nextEnd !== prevEnd) && nextAnchor) {
        setWindowAnchor(nextAnchor);
      }
      const page = currentPageAt(layout, layoutScroll, CURRENT_PAGE_ANCHOR_PX);
      setCurrent(page);
      setHovered((prev) => (prev === null ? prev : null));
      if (pinnedPage !== null) {
        const pinnedMetric = layout.byPageIndex.get(pinnedPage);
        if (pinnedMetric && pinnedMetric.order >= nextStart && pinnedMetric.order <= nextEnd) {
          setPinnedPage(null);
        }
      }
      pendingSavePageRef.current = page;
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        savePage(jobId, page);
        pendingSavePageRef.current = null;
      }, 500);
    });
  };

  // 打开时回到上次读到的页：用 byPageIndex 校验以兼容非连续 page.index
  const goToRef = useRef(goTo);
  useEffect(() => {
    goToRef.current = goTo;
  }, [goTo]);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const page = savedPage(jobId);
    if (page !== firstPageIndex && layoutRef.current.byPageIndex.has(page)) {
      restoreTimer.current = setTimeout(() => goToRef.current(page, 0, false), 400);
    }
    return () => clearTimeout(restoreTimer.current);
  }, [jobId, firstPageIndex]);

  // 从别处（如修改记录）打开的段落不在视野里时，按统一布局坐标滚到它所在的页
  useEffect(() => {
    if (!selectedSegment) return;
    const pageIndex = firstPageBySegment.get(selectedSegment);
    if (pageIndex === undefined) return;
    const metric = layoutRef.current.byPageIndex.get(pageIndex);
    if (!metric) return;
    const viewTop = layoutScrollRef.current;
    const viewBottom = viewTop + viewportHeightRef.current;
    if (metric.bottom < viewTop || metric.top > viewBottom) {
      goToRef.current(pageIndex, 0, false, 0);
    }
  }, [selectedSegment, firstPageBySegment, revealKey]);

  // 从术语面板定位：滚到术语所在位置并只高亮命中的词（只在 spot 变化时跳）。
  const docRef = useRef(doc);
  useEffect(() => {
    docRef.current = doc;
  }, [doc]);
  useEffect(() => {
    if (!spot) return;
    const d = docRef.current;
    const seg = d.segments.find((s) => s.id === spot.id);
    if (!seg) return;
    const block = spot.term
      ? seg.block_ids
          .map((id) => ({ block: blockById.get(id), text: texts.get(id) }))
          .find(({ text }) => {
            if (!text) return false;
            const source = spot.term?.source.trim().toLowerCase();
            const target = spot.term?.target.trim().toLowerCase();
            return Boolean(
              (source && text.sourceText.toLowerCase().includes(source)) ||
                (target && text.text.toLowerCase().includes(target)),
            );
          })?.block
      : blockById.get(seg.block_ids[0] ?? "");
    if (!block) return;
    // 用户手动定位优先于打开页面时尚未执行的阅读位置恢复。
    clearTimeout(restoreTimer.current);
    // 打开编辑侧栏会触发 ResizeObserver 和自适应缩放，等布局更新后再用最新比例定位。
    let nextFrame = 0;
    const raf = requestAnimationFrame(() => {
      nextFrame = requestAnimationFrame(() => {
        // 手机底部编辑面板会遮住一部分页面，段落起点靠近可见区顶部。
        goToRef.current(block.page, block.bbox.y0, true, window.innerWidth < NARROW ? 12 : 56);
        clearTimeout(flashTimer.current);
        const hasTerm = Boolean(
          spot.term &&
            ((spot.term.source && seg.source.toLowerCase().includes(spot.term.source.toLowerCase())) ||
              (spot.term.target && seg.target?.toLowerCase().includes(spot.term.target.toLowerCase()))),
        );
        setFlash(hasTerm ? null : spot.id);
        flashTimer.current = setTimeout(() => setFlash(null), 2400);
      });
    });
    return () => {
      cancelAnimationFrame(raf);
      cancelAnimationFrame(nextFrame);
    };
  }, [spot, texts, blockById]);

  const [picked, setPicked] = useState<number | null>(null); // 刚在目录里点的一项（同一页有好几节时高亮它）
  const goOutline = (item: OutlineData) => {
    setPicked(doc.outline.indexOf(item));
    jump(item.page, item.top);
    clearTimeout(flashTimer.current);
    setFlash(item.segment_id ?? null);
    flashTimer.current = setTimeout(() => setFlash(null), 2400);
    if (window.innerWidth < NARROW) setShowOutline(false); // 手机上点完收起
  };

  // 工具栏页码显示序号（1..doc.pages.length），双向映射到真实的 page.index
  const currentOrder = layout.byPageIndex.get(current)?.order ?? 0;

  // 合并窗口内页面与短距离平滑跳转的 pinnedPage，按文档顺序渲染且不重复
  const mountedPages = useMemo(() => {
    const list: PageData[] = [];
    const seen = new Set<number>();
    if (visibleEnd >= visibleStart) {
      for (let i = visibleStart; i <= visibleEnd; i++) {
        const p = doc.pages[i];
        if (p && !seen.has(p.index)) {
          seen.add(p.index);
          list.push(p);
        }
      }
    }
    if (pinnedPage !== null && !seen.has(pinnedPage)) {
      const metric = layout.byPageIndex.get(pinnedPage);
      const pinned = metric ? doc.pages[metric.order] : undefined;
      if (pinned) list.push(pinned);
    }
    return list;
  }, [doc.pages, visibleStart, visibleEnd, pinnedPage, layout]);

  const changeMode = (nextMode: ViewMode) => {
    setWindowAnchor(anchorRef.current);
    setMode(nextMode);
  };

  const changeZoom = (nextZoom: number | null) => {
    setWindowAnchor(anchorRef.current);
    setZoom(nextZoom);
  };

  const missingPages = doc.pages.filter((page) => page.missing_layout).map((page) => page.index + 1);

  return (
    <div className="reader">
      <div className="toolbar">
        {heading}
        <div className="toolbar-tools">
          <div className="segmented" role="group" aria-label="显示方式">
            {(
              [
                ["both", "对照"],
                ["translated", "译文"],
                ["original", "原文"],
              ] as const
            ).map(([m, label]) => (
              <button
                key={m}
                type="button"
                className={mode === m ? "on" : ""}
                aria-pressed={mode === m}
                onClick={() => changeMode(m)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="zoom" role="group" aria-label="缩放">
            <button
              type="button"
              className="quiet"
              onClick={() => changeZoom(Math.max(0.3, scale - 0.15))}
              aria-label="缩小"
            >
              −
            </button>
            <button type="button" className="quiet zoom-label" onClick={() => changeZoom(null)} title="适应宽度">
              {Math.round(scale * 100)}%
            </button>
            <button
              type="button"
              className="quiet"
              onClick={() => changeZoom(Math.min(4, scale + 0.15))}
              aria-label="放大"
            >
              +
            </button>
          </div>
          <label className="page-jump">
            第
            <input
              className="num"
              inputMode="numeric"
              aria-label="跳到第几页"
              value={pageDraft ?? String(currentOrder + 1)}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setPageDraft(e.target.value.replace(/\D/g, ""))}
              onBlur={() => setPageDraft(null)}
              onKeyDown={(e) => {
                if (isImeComposing(e) || e.key !== "Enter") return;
                const n = Number(pageDraft);
                const targetPage = n >= 1 && n <= doc.pages.length ? doc.pages[n - 1] : undefined;
                if (targetPage) goTo(targetPage.index);
                setPageDraft(null);
                e.currentTarget.blur();
              }}
            />
            / {doc.pages.length} 页
          </label>
          {doc.outline.length > 0 && (
            <button
              type="button"
              className={showOutline ? "quiet on" : "quiet"}
              aria-pressed={showOutline}
              onClick={() => setShowOutline((v) => !v)}
            >
              目录
            </button>
          )}
          <button
            type="button"
            className={searchOpen ? "quiet on" : "quiet"}
            aria-pressed={searchOpen}
            onClick={() => setSearchOpen((v) => !v)}
            title="搜索原文和译文"
          >
            搜索
          </button>
          {actions}
        </div>
      </div>
      {searchOpen && (
        <div className="reader-search" role="search" aria-label="全文搜索">
          <label className="reader-search-input">
            <span>全文搜索</span>
            <input
              autoFocus
              type="search"
              value={searchQuery}
              placeholder="搜索原文或译文"
              onChange={(e) => onSearchChange(e.target.value)}
              onKeyDown={(e) => {
                if (isImeComposing(e) || e.key !== "Enter") return;
                e.preventDefault();
                if (debouncedQuery !== searchQuery) {
                  clearTimeout(searchTimer.current);
                  setDebouncedQuery(searchQuery);
                  const immediate = querySearchIndex(searchIndexData, searchQuery);
                  moveSearch(e.shiftKey ? -1 : 1, immediate);
                } else {
                  moveSearch(e.shiftKey ? -1 : 1);
                }
              }}
              aria-label="搜索原文或译文"
            />
          </label>
          <span className="reader-search-count" aria-live="polite">
            {debouncedQuery.trim()
              ? searchResults.length
                ? `${activeSearchIndex + 1} / ${searchResults.length}`
                : "没有结果"
              : searchQuery.trim()
                ? "搜索中…"
                : "输入关键词"}
          </span>
          <button
            type="button"
            className="quiet"
            onClick={() => moveSearch(-1)}
            disabled={!searchResults.length}
            aria-label="上一个结果"
          >
            ↑
          </button>
          <button
            type="button"
            className="quiet"
            onClick={() => moveSearch(1)}
            disabled={!searchResults.length}
            aria-label="下一个结果"
          >
            ↓
          </button>
          <button type="button" className="quiet" onClick={() => setSearchOpen(false)}>
            关闭
          </button>
          {searchResults.length > 0 && (
            <ol className="reader-search-results">
              {searchResults.slice(0, 50).map((hit, index) => (
                <li key={hit.segmentId}>
                  <button
                    type="button"
                    className={index === activeSearchIndex ? "search-result on" : "search-result"}
                    onClick={() => jumpSearchHit(hit, index)}
                  >
                    <span>第 {(layout.byPageIndex.get(hit.page)?.order ?? hit.page) + 1} 页</span>
                    <strong>{shortLabel(hit.target || hit.source, 90)}</strong>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {missingPages.length > 0 && (
        <p className="notice" role="status">
          第 {missingPages.join("、")} 页未返回版面解析结果，已保留原文。
        </p>
      )}
      <div className="reader-body">
        {showOutline && (
          <OutlinePanel
            items={doc.outline}
            current={current}
            picked={picked !== null && doc.outline[picked]?.page === current ? picked : null}
            onGo={goOutline}
            onClose={() => setShowOutline(false)}
          />
        )}
        <div className="pages" ref={scroller} onScroll={onScroll}>
          {(original.error || background.error) && (
            <p className="error">PDF 加载失败：{original.error ?? background.error}</p>
          )}
          <div className="pages-canvas" style={{ height: layout.totalHeight, minWidth: contentMinWidth }}>
            {mountedPages.map((page) => {
              const metric = layout.byPageIndex.get(page.index);
              const pageTop = metric?.top ?? 0;
              const pageHeight = metric?.height ?? page.height * scale;
              const pageHovered = hovered && pagesBySegment.get(hovered)?.has(page.index) ? hovered : null;
              const pageSelected =
                selectedSegment && pagesBySegment.get(selectedSegment)?.has(page.index) ? selectedSegment : null;
              const pageFlash = flash && pagesBySegment.get(flash)?.has(page.index) ? flash : null;
              const pageCurrentSearch =
                currentSearchId && pagesBySegment.get(currentSearchId)?.has(page.index) ? currentSearchId : null;
              return (
                <PageRow
                  key={page.index}
                  page={page}
                  top={pageTop}
                  rowHeight={pageHeight}
                  blocks={blocksByPage.get(page.index) ?? EMPTY_BLOCKS}
                  scale={scale}
                  mode={mode}
                  original={original.doc}
                  background={background.doc}
                  textLayerFailed={failedTextPages.has(page.index)}
                  onTextLayerError={handleTextLayerError}
                  texts={texts}
                  hovered={pageHovered}
                  flash={pageFlash}
                  marked={markedByPage.get(page.index)}
                  selected={pageSelected}
                  fontsVersion={fontsVersion}
                  side={side}
                  searchIds={searchIdsByPage.get(page.index)}
                  currentSearchId={pageCurrentSearch}
                  onHover={setHovered}
                  onSelect={onSelectSegment}
                  pageLinks={byPage.get(page.index)}
                  blockLinks={byBlock}
                  jumpTarget={jumpTarget?.page === page.index ? jumpTarget : null}
                  term={spot?.term}
                  onJump={jump}
                />
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

const EMPTY_BLOCKS: readonly BlockData[] = [];

interface RowProps {
  page: PageData;
  top: number;
  rowHeight: number;
  blocks: readonly BlockData[];
  scale: number;
  mode: ViewMode;
  original: PDFDocumentProxy | null;
  background: PDFDocumentProxy | null;
  textLayerFailed: boolean;
  onTextLayerError: (pageIndex: number) => void;
  texts: Map<string, BlockText>;
  hovered: string | null;
  flash: string | null;
  marked: ReadonlySet<string> | undefined;
  selected: string | null;
  fontsVersion: number;
  side: "left" | "right";
  searchIds: ReadonlySet<string> | undefined;
  currentSearchId: string | null;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
  pageLinks: LinkData[] | undefined;
  blockLinks: Map<string, LinkData[]>;
  jumpTarget: JumpTarget | null;
  term: { source: string; target: string } | undefined;
  onJump: (page: number, top: number) => void;
}

function groupLinks(links: LinkData[]) {
  const byPage = new Map<number, LinkData[]>();
  const byBlock = new Map<string, LinkData[]>();
  for (const l of links) {
    byPage.set(l.page, [...(byPage.get(l.page) ?? []), l]);
    if (l.block_id && l.text) byBlock.set(l.block_id, [...(byBlock.get(l.block_id) ?? []), l]);
  }
  return { byPage, byBlock };
}

/** 按原位置铺在页面上的链接热区（canvas 上的链接点不了）。 */
function LinkLayer({
  links,
  scale,
  onJump,
}: {
  links: LinkData[];
  scale: number;
  onJump: (p: number, t: number) => void;
}) {
  return (
    <>
      {links.map((l, i) => (
        <a
          key={i}
          className="pdf-link"
          href={l.uri || "#"}
          target={l.uri ? "_blank" : undefined}
          rel="noreferrer"
          title={l.uri || (l.dest_page !== null ? `跳到第 ${l.dest_page + 1} 页` : undefined)}
          style={{
            left: l.rect.x0 * scale,
            top: l.rect.y0 * scale,
            width: (l.rect.x1 - l.rect.x0) * scale,
            height: (l.rect.y1 - l.rect.y0) * scale,
          }}
          onClick={(e) => {
            e.stopPropagation();
            if (!l.uri) {
              e.preventDefault();
              if (l.dest_page !== null) onJump(l.dest_page, l.dest_top);
            }
          }}
        />
      ))}
    </>
  );
}

function JumpMarker({ top, scale }: { top: number; scale: number }) {
  return (
    <div
      className="page-jump-flash"
      style={{ top: top * scale, height: Math.max(28, 36 * scale) }}
      aria-hidden="true"
    />
  );
}

function highlightPlainText(text: string, needle: string | undefined, key: string): ReactNode {
  const value = needle?.trim();
  if (!value) return text;
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(escaped, "gi");
  const parts: ReactNode[] = [];
  let at = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = re.exec(text))) {
    if (match.index > at) parts.push(text.slice(at, match.index));
    parts.push(
      <mark className="term-highlight" key={`${key}-${index}`}>
        {match[0]}
      </mark>,
    );
    at = match.index + match[0].length;
    index += 1;
  }
  return at === 0 ? text : [...parts, text.slice(at)];
}

const PageRow = memo(function PageRow({
  page,
  top,
  rowHeight,
  blocks,
  scale,
  mode,
  original,
  background,
  textLayerFailed,
  onTextLayerError,
  texts,
  hovered,
  flash,
  marked,
  selected,
  fontsVersion,
  side,
  searchIds,
  currentSearchId,
  onHover,
  onSelect,
  pageLinks,
  blockLinks,
  jumpTarget,
  term,
  onJump,
}: RowProps) {
  const size = { width: page.width * scale, height: page.height * scale };
  const hoveredSegRef = useRef<string | null>(null);
  const pointerDownRef = useRef<{ x: number; y: number } | null>(null);
  const mark = (segId: string) =>
    (segId === selected ? " selected" : segId === hovered ? " hovered" : "") +
    (segId === flash ? " flash" : marked?.has(segId) ? " marked" : "") +
    (searchIds?.has(segId) ? " search-hit" : "") +
    (currentSearchId === segId ? " search-current" : "");

  const useNativeTextLayer = Boolean(original && page.kind === "native" && !textLayerFailed);
  const handleError = useCallback(() => onTextLayerError(page.index), [onTextLayerError, page.index]);

  const segmentAtPointer = (e: React.MouseEvent<HTMLDivElement>): string | null => {
    const rect = e.currentTarget.getBoundingClientRect();
    const effectiveScale = page.width > 0 && rect.width > 0 ? rect.width / page.width : scale;
    if (effectiveScale <= 0) return null;
    const xPt = (e.clientX - rect.left) / effectiveScale;
    const yPt = (e.clientY - rect.top) / effectiveScale;
    const block = hitBlockAt(blocks, xPt, yPt);
    return block ? (texts.get(block.id)?.segment.id ?? null) : null;
  };

  const onOriginalPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    pointerDownRef.current = { x: e.clientX, y: e.clientY };
  };

  const onOriginalMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const segId = segmentAtPointer(e);
    if (segId === hoveredSegRef.current) return;
    hoveredSegRef.current = segId;
    onHover(segId);
  };

  const onOriginalMouseLeave = () => {
    pointerDownRef.current = null;
    if (hoveredSegRef.current === null && hovered === null) return;
    hoveredSegRef.current = null;
    onHover(null);
  };

  const onOriginalClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const down = pointerDownRef.current;
    pointerDownRef.current = null;
    // 拖拽划词刚松开或存在非空选区时，不触发段落编辑选中
    if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > DRAG_THRESHOLD_PX) return;
    if (window.getSelection()?.toString()) return;
    const segId = segmentAtPointer(e);
    if (segId) onSelect(segId);
  };

  return (
    <div
      className={`page-row${mode === "both" ? "" : " single"}${side === "left" ? " side-left" : ""}`}
      data-page={page.index}
      style={{ height: rowHeight, transform: `translateY(${top}px)` }}
    >
      {mode !== "translated" && (
        <div
          className="page"
          style={size}
          aria-label={`原文第 ${page.index + 1} 页`}
          onPointerDown={onOriginalPointerDown}
          onMouseMove={onOriginalMouseMove}
          onMouseLeave={onOriginalMouseLeave}
          onClick={onOriginalClick}
        >
          {original && <PdfCanvas doc={original} page={page.index} scale={scale} />}
          <div className="hit-layer" aria-hidden="true">
            {blocks.map((b) => {
              const t = texts.get(b.id);
              if (!t) return null;
              return (
                <div
                  key={b.id}
                  className={`hit${mark(t.segment.id)}`}
                  style={{
                    left: b.bbox.x0 * scale,
                    top: b.bbox.y0 * scale,
                    width: (b.bbox.x1 - b.bbox.x0) * scale,
                    height: (b.bbox.y1 - b.bbox.y0) * scale,
                  }}
                />
              );
            })}
          </div>
          {useNativeTextLayer && original ? (
            <PdfTextLayer
              doc={original}
              page={page.index}
              scale={scale}
              highlight={term?.source}
              onError={handleError}
            />
          ) : (
            <div className="source-text-layer" aria-label={`原文第 ${page.index + 1} 页文字`}>
              {blocks.map((b) => {
                const t = texts.get(b.id);
                return t ? (
                  <div
                    key={b.id}
                    className="source-text"
                    style={{
                      left: b.bbox.x0 * scale,
                      top: b.bbox.y0 * scale,
                      width: (b.bbox.x1 - b.bbox.x0) * scale,
                      height: (b.bbox.y1 - b.bbox.y0) * scale,
                      fontSize: b.font_size * scale,
                    }}
                    aria-label={t.sourceText}
                  >
                    {highlightPlainText(
                      t.sourceText.replace(/<m\d+\/>/g, "[公式]").replace(/<\/?(?:sup|sub|b|i)>/g, ""),
                      term?.source,
                      `source-${b.id}`,
                    )}
                  </div>
                ) : null;
              })}
            </div>
          )}
          {pageLinks && <LinkLayer links={pageLinks} scale={scale} onJump={onJump} />}
          {jumpTarget && <JumpMarker top={jumpTarget.top} scale={scale} />}
        </div>
      )}
      {mode !== "original" && (
        <div className="page" style={size} aria-label={`译文第 ${page.index + 1} 页`}>
          {background && <PdfCanvas doc={background} page={page.index} scale={scale} />}
          {jumpTarget && <JumpMarker top={jumpTarget.top} scale={scale} />}
          <div className="overlay" style={{ width: page.width * scale, height: page.height * scale }}>
            {blocks.map((b) => {
              const t = texts.get(b.id);
              return t ? (
                <OverlayBlock
                  key={b.id}
                  item={t}
                  className={mark(t.segment.id)}
                  scale={scale}
                  fontsVersion={fontsVersion}
                  onHover={onHover}
                  onSelect={onSelect}
                  links={blockLinks.get(b.id)}
                  highlight={term?.target}
                  onJump={onJump}
                />
              ) : null;
            })}
          </div>
          {pageLinks && <LinkLayer links={pageLinks.filter((l) => !l.block_id)} scale={scale} onJump={onJump} />}
        </div>
      )}
    </div>
  );
});

/** 网页字体加载完成的次数：字体换上后要重新拟合字号（先用后备字体测出的结果不准）。 */
function useFontsVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const fonts = document.fonts as FontFaceSet | undefined;
    if (!fonts) return;
    const bump = () => setVersion((v) => v + 1);
    fonts.addEventListener("loadingdone", bump);
    return () => fonts.removeEventListener("loadingdone", bump);
  }, []);
  return version;
}

function OverlayBlock({
  item,
  className,
  scale,
  fontsVersion,
  onHover,
  onSelect,
  links,
  highlight,
  onJump,
}: {
  item: BlockText;
  className: string;
  scale: number;
  fontsVersion: number;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
  links: LinkData[] | undefined;
  highlight?: string;
  onJump: (page: number, top: number) => void;
}) {
  const { block, segment, text, translated, first } = item;
  const box = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  const role = roleOf(block, translated);
  const rect = layoutBox(block);
  // 排版规则（clreq）：中西文之间加空格、正文段首缩进两字；没有译文时显示原文，不做处理
  const normalized = dropFakeScripts(text);
  const shown = translated ? spaceCjkLatin(normalized) : normalized;
  const indent = translated && wantsIndent(block, segment, first);
  const [captionNo, rest] = role === "caption" && first ? splitCaptionNo(shown) : ["", shown];
  // 图注编号单独渲染，链接的“第几处”要扣掉编号里的出现次数
  const inline = useMemo<InlineLink[]>(
    () =>
      (links ?? []).map((l) => ({
        text: l.text,
        nth: l.nth - countCompact(captionNo, l.text),
        href: l.uri || undefined,
        onActivate: l.dest_page !== null ? () => onJump(l.dest_page ?? 0, l.dest_top) : undefined,
      })),
    [links, captionNo, onJump],
  );

  // 内容（含 KaTeX）渲染后拟合字号。直接按显示尺寸排版（1pt = scale 像素），不用 transform 缩放：
  // 先排小字再放大，或先排大字再缩小，浏览器都会把字宽对齐到像素，英文字母之间出现空隙。
  useLayoutEffect(() => {
    if (box.current && inner.current) {
      setOverflow(fitElement(box.current, inner.current, block.font_size * scale).overflow);
    }
  }, [text, block.font_size, scale, fontsVersion]);

  return (
    <div
      ref={box}
      className={`tblock role-${role}${translated ? "" : " untranslated"}${overflow ? " overflow" : ""}${className}`}
      style={{
        left: rect.x0 * scale,
        top: rect.y0 * scale,
        width: (rect.x1 - rect.x0) * scale,
        height: (rect.y1 - rect.y0) * scale,
        color: `#${block.color.toString(16).padStart(6, "0")}`,
      }}
      title={overflow ? "译文放不下，点击查看全文" : undefined}
      onMouseEnter={() => onHover(segment.id)}
      onMouseLeave={() => onHover(null)}
      onClick={() => {
        if (!window.getSelection()?.toString()) onSelect(segment.id);
      }}
      tabIndex={first ? 0 : -1}
      role="button"
      aria-label={`段落：${shortLabel(segment.source)}`}
      data-seg={first ? segment.id : undefined}
      onKeyDown={keyActivate(() => onSelect(segment.id))}
      onFocus={() => onHover(segment.id)}
      onBlur={() => onHover(null)}
    >
      <div ref={inner} className={indent ? "indent" : undefined}>
        {captionNo && (
          <span className="no">
            <InlineMathText text={captionNo} formulas={{}} fallbackUrl={() => ""} />
          </span>
        )}
        <InlineMathText
          text={rest}
          formulas={segment.formulas}
          fallbackUrl={(k) => formulaUrl(segment.id, k)}
          links={inline}
          highlight={highlight}
        />
      </div>
    </div>
  );
}
