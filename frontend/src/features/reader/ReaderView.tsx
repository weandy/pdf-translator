/**
 * 阅读器（docs/03-mvp-design.md §4.2）：每一行是同一页的原文与译文，共用一个滚动条，天然对齐。
 * 译文页 = 去字背景页（pdf.js 渲染）+ 叠加层（按 pt 坐标排版，整体 transform 缩放，拟合结果与缩放无关）。
 */
import type { PDFDocumentProxy } from "pdfjs-dist";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import "./fonts.css";

import { blockTexts, layoutBox, roleOf, type BlockText } from "@/entities/document/model";
import { fileUrl, formulaUrl, type DocumentData, type PageData } from "@/shared/api/client";
import { fitElement } from "@/shared/fit/fit";
import { InlineMathText } from "@/shared/math/inline";
import { splitCaptionNo } from "@/shared/text/script";
import { PdfCanvas, usePdf } from "@/shared/pdf/pdf";

export type ViewMode = "both" | "translated" | "original";

interface Props {
  doc: DocumentData;
  jobId: string;
  selectedSegment: string | null;
  onSelectSegment: (id: string) => void;
  /** 工具栏左侧（标题等）与右侧（下载等），由页面组合 */
  heading?: ReactNode;
  actions?: ReactNode;
}

const PADDING = 48; // .pages 左右内边距之和
const SPINE = 2; // 对照模式两页之间的装订线

export function ReaderView({ doc, jobId, selectedSegment, onSelectSegment, heading, actions }: Props) {
  const [mode, setMode] = useState<ViewMode>("both");
  const [zoom, setZoom] = useState<number | null>(null); // null = 自适应宽度
  const [width, setWidth] = useState(1200);
  const [hovered, setHovered] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const original = usePdf(mode !== "translated" ? fileUrl(jobId, "original.pdf") : null);
  const background = usePdf(mode !== "original" ? fileUrl(jobId, "background.pdf") : null);
  const texts = useMemo(() => blockTexts(doc), [doc]);
  const fontsVersion = useFontsVersion();

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

  return (
    <div className="reader">
      <div className="toolbar">
        {heading}
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
          <button type="button" className="quiet" onClick={() => setZoom(Math.min(4, scale + 0.15))} aria-label="放大">
            +
          </button>
        </div>
        {actions}
      </div>
      <div className="pages" ref={scroller}>
        {(original.error || background.error) && <p className="error">PDF 加载失败：{original.error ?? background.error}</p>}
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
            selected={selectedSegment}
            fontsVersion={fontsVersion}
            onHover={setHovered}
            onSelect={onSelectSegment}
          />
        ))}
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
  selected: string | null;
  fontsVersion: number;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
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

function PageRow({
  page,
  scale,
  mode,
  original,
  background,
  texts,
  hovered,
  selected,
  fontsVersion,
  onHover,
  onSelect,
}: RowProps) {
  const [ref, near] = useNearViewport<HTMLDivElement>();
  const size = { width: page.width * scale, height: page.height * scale };
  const blocks = page.blocks.filter((b) => texts.has(b.id));
  const mark = (segId: string) => (segId === selected ? " selected" : segId === hovered ? " hovered" : "");

  return (
    <div className={`page-row${mode === "both" ? "" : " single"}`} ref={ref}>
      {mode !== "translated" && (
        <div className="page" style={size} aria-label={`原文第 ${page.index + 1} 页`}>
          {near && original && <PdfCanvas doc={original} page={page.index} scale={scale} />}
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
                onClick={() => onSelect(t.segment.id)}
              />
            );
          })}
        </div>
      )}
      {mode !== "original" && (
        <div className="page" style={size} aria-label={`译文第 ${page.index + 1} 页`}>
          {near && background && <PdfCanvas doc={background} page={page.index} scale={scale} />}
          {near && (
            <div
              className="overlay"
              style={{ width: page.width * scale, height: page.height * scale }}
            >
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
                  />
                ) : null;
              })}
            </div>
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
}: {
  item: BlockText;
  className: string;
  scale: number;
  fontsVersion: number;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
}) {
  const { block, segment, text, translated, first } = item;
  const box = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  const role = roleOf(block);
  const rect = layoutBox(block);
  const [captionNo, rest] = role === "caption" && first ? splitCaptionNo(text) : ["", text];

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
      onClick={() => onSelect(segment.id)}
    >
      <div ref={inner}>
        {captionNo && (
          <span className="no">
            <InlineMathText text={captionNo} formulas={{}} fallbackUrl={() => ""} />
          </span>
        )}
        <InlineMathText text={rest} formulas={segment.formulas} fallbackUrl={(k) => formulaUrl(segment.id, k)} />
      </div>
    </div>
  );
}
