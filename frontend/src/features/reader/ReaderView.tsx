/**
 * 阅读器（docs/03-mvp-design.md §4.2）：每一行是同一页的原文与译文，共用一个滚动条，天然对齐。
 * 译文页 = 去字背景页（pdf.js 渲染）+ 叠加层（按 pt 坐标排版，整体 transform 缩放，拟合结果与缩放无关）。
 */
import type { PDFDocumentProxy } from "pdfjs-dist/legacy/build/pdf.mjs";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import "./fonts.css";

import { blockTexts, layoutBox, roleOf, type BlockText } from "@/entities/document/model";
import {
  fileUrl,
  formulaUrl,
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
import { PdfCanvas, usePdf } from "@/shared/pdf/pdf";

import { OutlinePanel } from "./OutlinePanel";
import { searchDocument, type SearchHit } from "./search";

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
  // 窄屏（手机）上默认只显示译文、适应宽度；用户切换后以用户的选择为准
  const [mode, setMode] = useState<ViewMode>(() => (window.innerWidth < NARROW ? "translated" : "both"));
  const [zoom, setZoom] = useState<number | null>(null); // null = 自适应宽度
  const [width, setWidth] = useState(1200);
  const [hovered, setHovered] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null); // 刚跳到的标题段落：原文与译文上短暂高亮
  const flashTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const jumpTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const [jumpTarget, setJumpTarget] = useState<JumpTarget | null>(null);
  const [showOutline, setShowOutline] = useState(() => window.innerWidth >= 1280 && doc.outline.length > 0);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchIndex, setSearchIndex] = useState(0);
  const [current, setCurrent] = useState(0); // 当前页（0 起）
  const [pageDraft, setPageDraft] = useState<string | null>(null); // 正在输入的页码
  const scroller = useRef<HTMLDivElement>(null);
  const frame = useRef(0);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const restoreTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const restored = useRef(false);

  // 只加载当前显示方式用得到的 PDF
  const original = usePdf(mode === "both" || mode === "original" ? fileUrl(jobId, "original.pdf") : null);
  const background = usePdf(mode === "both" || mode === "translated" ? fileUrl(jobId, "background.pdf") : null);
  const texts = useMemo(() => blockTexts(doc), [doc]);
  // 链接：原文页上全部按原位置；译文页上，译文块外的按原位置，块内的落在译文文字上
  const { byPage, byBlock } = useMemo(() => groupLinks(doc.links), [doc.links]);
  const fontsVersion = useFontsVersion();

  // 从别处（如修改记录）打开的段落不在视野里时，滚到它所在的页；在阅读器里点选的本来就在视野里，不滚动
  useEffect(() => {
    const el = scroller.current;
    const seg = doc.segments.find((s) => s.id === selectedSegment);
    const page = doc.pages.find((p) => p.blocks.some((b) => b.id === seg?.block_ids[0]));
    const row = el && page ? el.querySelector<HTMLElement>(`.page-row[data-page="${page.index}"]`) : null;
    if (!el || !row) return;
    const view = el.getBoundingClientRect();
    const box = row.getBoundingClientRect();
    if (box.bottom < view.top || box.top > view.bottom) row.scrollIntoView({ block: "start" });
  }, [selectedSegment, doc, revealKey]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const maxPageWidth = Math.max(...doc.pages.map((p) => p.width));
  const columns = mode === "both" ? 2 : 1;
  const fitZoom = Math.max(0.3, (width - PADDING - (columns - 1) * SPINE) / columns / maxPageWidth);
  const scale = zoom ?? fitZoom;
  const searchResults = useMemo(() => searchDocument(doc, searchQuery), [doc, searchQuery]);
  const searchIds = useMemo(() => new Set(searchResults.map((hit) => hit.segmentId)), [searchResults]);
  const activeSearchIndex = Math.min(searchIndex, Math.max(0, searchResults.length - 1));
  const currentSearchId = searchResults[activeSearchIndex]?.segmentId ?? null;

  useEffect(() => {
    if (!searchOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSearchOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [searchOpen]);

  // 跳到某页的某个位置（top 为离页顶的 PDF 点）
  const goTo = useCallback(
    (page: number, top = 0, smooth = true, inset = 56) => {
      const el = scroller.current;
      const row = el?.querySelector<HTMLElement>(`.page-row[data-page="${page}"]`);
      if (!el || !row) return;
      const offset = row.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
      // 滚动条、侧栏会改变实际页宽；布局已更新但 effect 中的比例可能还未同步。
      const pageWidth = doc.pages.find((p) => p.index === page)?.width;
      const shownWidth = row.querySelector<HTMLElement>(".page")?.getBoundingClientRect().width;
      const shownScale = pageWidth && shownWidth ? shownWidth / pageWidth : scale;
      el.scrollTo({ top: Math.max(offset + top * shownScale - inset, 0), behavior: smooth ? "smooth" : "auto" });
    },
    [scale, doc.pages],
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

  const jumpSearch = useCallback(
    (hit: SearchHit) => {
      const segment = doc.segments.find((s) => s.id === hit.segmentId);
      const block = doc.pages.flatMap((p) => p.blocks).find((b) => b.id === segment?.block_ids[0]);
      if (!block) return;
      setSearchIndex(searchResults.findIndex((item) => item.segmentId === hit.segmentId));
      jump(block.page, block.bbox.y0);
      setFlash(hit.segmentId);
      clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(null), 2400);
    },
    [doc, jump, searchResults],
  );

  const moveSearch = (delta: number) => {
    if (!searchResults.length) return;
    const next = (activeSearchIndex + delta + searchResults.length) % searchResults.length;
    setSearchIndex(next);
    const hit = searchResults[next];
    if (hit) jumpSearch(hit);
  };

  // 滚动时算出当前页（工具栏页码、目录高亮），并记下来，下次打开回到这里
  const onScroll = () => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      const el = scroller.current;
      if (!el) return;
      const limit = el.getBoundingClientRect().top + 80;
      let page = 0;
      for (const m of el.querySelectorAll<HTMLElement>(".page-row")) {
        if (m.getBoundingClientRect().top > limit) break;
        page = Number(m.dataset.page ?? 0);
      }
      setCurrent(page);
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => savePage(jobId, page), 500);
    });
  };

  // 打开时回到上次读到的页：等缩放比例稳定后再跳（期间 goTo 会随缩放变化，用引用拿最新的）
  const goToRef = useRef(goTo);
  useEffect(() => {
    goToRef.current = goTo;
  }, [goTo]);
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const page = savedPage(jobId);
    if (page > 0 && page < doc.pages.length)
      restoreTimer.current = setTimeout(() => goToRef.current(page, 0, false), 400);
    return () => clearTimeout(restoreTimer.current);
  }, [jobId, doc.pages.length]);

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
    const blocks = d.pages.flatMap((p) => p.blocks);
    const block = spot.term
      ? seg.block_ids
          .map((id) => ({ block: blocks.find((item) => item.id === id), text: texts.get(id) }))
          .find(({ text }) => {
            if (!text) return false;
            const source = spot.term?.source.trim().toLocaleLowerCase();
            const target = spot.term?.target.trim().toLocaleLowerCase();
            return Boolean(
              (source && text.sourceText.toLocaleLowerCase().includes(source)) ||
                (target && text.text.toLocaleLowerCase().includes(target)),
            );
          })?.block
      : blocks.find((b) => b.id === seg.block_ids[0]);
    if (!block) return;
    // 用户手动定位优先于打开页面时尚未执行的阅读位置恢复。
    clearTimeout(restoreTimer.current);
    // 打开编辑侧栏会触发 ResizeObserver 和自适应缩放，等布局更新后再用最新比例定位。
    let nextFrame = 0;
    const frame = requestAnimationFrame(() => {
      nextFrame = requestAnimationFrame(() => {
        // 手机底部编辑面板会遮住一部分页面，段落起点靠近可见区顶部。
        goToRef.current(block.page, block.bbox.y0, true, window.innerWidth < NARROW ? 12 : 56);
        clearTimeout(flashTimer.current);
        const hasTerm = Boolean(
          spot.term &&
            ((spot.term.source && seg.source.toLocaleLowerCase().includes(spot.term.source.toLocaleLowerCase())) ||
              (spot.term.target && seg.target?.toLocaleLowerCase().includes(spot.term.target.toLocaleLowerCase()))),
        );
        setFlash(hasTerm ? null : spot.id);
        flashTimer.current = setTimeout(() => setFlash(null), 2400);
      });
    });
    return () => {
      cancelAnimationFrame(frame);
      cancelAnimationFrame(nextFrame);
    };
  }, [spot, texts]);

  const [picked, setPicked] = useState<number | null>(null); // 刚在目录里点的一项（同一页有好几节时高亮它）
  const goOutline = (item: OutlineData) => {
    setPicked(doc.outline.indexOf(item));
    jump(item.page, item.top);
    clearTimeout(flashTimer.current);
    setFlash(item.segment_id ?? null);
    flashTimer.current = setTimeout(() => setFlash(null), 2400);
    if (window.innerWidth < NARROW) setShowOutline(false); // 手机上点完收起
  };

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
                onClick={() => setMode(m)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="zoom" role="group" aria-label="缩放">
            <button
              type="button"
              className="quiet"
              onClick={() => setZoom(Math.max(0.3, scale - 0.15))}
              aria-label="缩小"
            >
              −
            </button>
            <button type="button" className="quiet zoom-label" onClick={() => setZoom(null)} title="适应宽度">
              {Math.round(scale * 100)}%
            </button>
            <button
              type="button"
              className="quiet"
              onClick={() => setZoom(Math.min(4, scale + 0.15))}
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
              value={pageDraft ?? String(current + 1)}
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setPageDraft(e.target.value.replace(/\D/g, ""))}
              onBlur={() => setPageDraft(null)}
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                const n = Number(pageDraft);
                if (n >= 1 && n <= doc.pages.length) goTo(n - 1);
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
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  moveSearch(e.shiftKey ? -1 : 1);
                }
              }}
              aria-label="搜索原文或译文"
            />
          </label>
          <span className="reader-search-count" aria-live="polite">
            {searchQuery.trim()
              ? searchResults.length
                ? `${activeSearchIndex + 1} / ${searchResults.length}`
                : "没有结果"
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
                    onClick={() => jumpSearch(hit)}
                  >
                    <span>第 {hit.page + 1} 页</span>
                    <strong>{shortLabel(hit.target || hit.source, 90)}</strong>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
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
          {doc.pages.map((page) => (
            <PageRow
              key={page.index}
              page={page}
              scale={scale}
              mode={mode}
              original={original.doc}
              background={background.doc}
              texts={texts}
              hovered={hovered}
              flash={flash}
              marked={marked}
              selected={selectedSegment}
              fontsVersion={fontsVersion}
              side={side}
              searchIds={searchIds}
              currentSearchId={currentSearchId}
              onHover={setHovered}
              onSelect={onSelectSegment}
              pageLinks={byPage.get(page.index)}
              blockLinks={byBlock}
              jumpTarget={jumpTarget?.page === page.index ? jumpTarget : null}
              term={spot?.term}
              onJump={jump}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  page: PageData;
  scale: number;
  mode: ViewMode;
  original: PDFDocumentProxy | null;
  background: PDFDocumentProxy | null;
  texts: Map<string, BlockText>;
  hovered: string | null;
  flash: string | null;
  marked: ReadonlySet<string> | undefined;
  selected: string | null;
  fontsVersion: number;
  side: "left" | "right";
  searchIds: ReadonlySet<string>;
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

/** 只在接近可视区时渲染 canvas，大文档也不卡。 */
function useNearViewport<T extends HTMLElement>(): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setNear(Boolean(e?.isIntersecting)), { rootMargin: "1200px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return [ref, near];
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

function PageRow({
  page,
  scale,
  mode,
  original,
  background,
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
  const [ref, near] = useNearViewport<HTMLDivElement>();
  const size = { width: page.width * scale, height: page.height * scale };
  const blocks = page.blocks.filter((b) => texts.has(b.id));
  const mark = (segId: string) =>
    (segId === selected ? " selected" : segId === hovered ? " hovered" : "") +
    (segId === flash ? " flash" : marked?.has(segId) ? " marked" : "") +
    (searchIds.has(segId) ? " search-hit" : "") +
    (currentSearchId === segId ? " search-current" : "");

  const selectSegment = (id: string) => {
    if (window.getSelection()?.toString()) return;
    onSelect(id);
  };

  return (
    <div
      className={`page-row${mode === "both" ? "" : " single"}${side === "left" ? " side-left" : ""}`}
      ref={ref}
      data-page={page.index}
    >
      {mode !== "translated" && (
        <div className="page" style={size} aria-label={`原文第 ${page.index + 1} 页`}>
          {near && original && <PdfCanvas doc={original} page={page.index} scale={scale} />}
          {jumpTarget && <JumpMarker top={jumpTarget.top} scale={scale} />}
          {near && (
            <div className="source-text-layer" aria-label={`原文第 ${page.index + 1} 页文字`}>
              {blocks.map((b) => {
                const t = texts.get(b.id);
                return t ? (
                  <div
                    key={b.id}
                    className={`source-text${mark(t.segment.id)}`}
                    style={{
                      left: b.bbox.x0 * scale,
                      top: b.bbox.y0 * scale,
                      width: (b.bbox.x1 - b.bbox.x0) * scale,
                      height: (b.bbox.y1 - b.bbox.y0) * scale,
                      fontSize: b.font_size * scale,
                    }}
                    aria-label={t.sourceText}
                    onClick={() => selectSegment(t.segment.id)}
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
                onMouseEnter={() => onHover(t.segment.id)}
                onMouseLeave={() => onHover(null)}
                onClick={() => selectSegment(t.segment.id)}
              />
            );
          })}
          {near && pageLinks && <LinkLayer links={pageLinks} scale={scale} onJump={onJump} />}
        </div>
      )}
      {mode !== "original" && (
        <div className="page" style={size} aria-label={`译文第 ${page.index + 1} 页`}>
          {near && background && <PdfCanvas doc={background} page={page.index} scale={scale} />}
          {jumpTarget && <JumpMarker top={jumpTarget.top} scale={scale} />}
          {near && (
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
          )}
          {near && pageLinks && (
            <LinkLayer links={pageLinks.filter((l) => !l.block_id)} scale={scale} onJump={onJump} />
          )}
        </div>
      )}
    </div>
  );
}

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
  const role = roleOf(block);
  const rect = layoutBox(block);
  // 排版规则（clreq）：中西文之间加空格、正文段首缩进两字；没有译文时显示原文，不做处理
  const shown = dropFakeScripts(translated ? spaceCjkLatin(text) : text);
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
