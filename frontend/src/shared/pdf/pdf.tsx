/**
 * pdf.js 封装：加载文档（同一地址只加载一次）、渲染单页到 canvas、统计本地文件页数。
 * useWasm=false：WebAssembly 解码需要放宽 CSP，改用纯 JS 解码器。扫描件常用的 CCITT 传真、JBIG2、JPEG 2000 图片
 * 的解码器按 wasmUrl 加载（构建时原名放在 /pdfjs/，见 vite.config.ts），不给这个地址扫描页会画成空白。
 * 用 legacy 构建：默认构建直接调用 Promise.try、Map.getOrInsertComputed 等 2025 年后才有的 API，
 * 稍旧的 Chrome 上整页画不出来（原文页空白、译文页没有图片），legacy 构建自带补丁。
 */
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist/legacy/build/pdf.mjs";
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState, type CSSProperties } from "react";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const DECODERS = "/pdfjs/";
// 一个任务最多同时使用原文和译文两份 PDF；保留当前任务和最近 3 个任务。
export const MAX_CACHED_PDFS = 8;

// 单张 Canvas 背板像素与边长预算：桌面约 24 MiB、移动端约 16 MiB（对照双画布分别 ≤ 48 / 32 MiB），
// 仅限制内部绘制像素，不改变 CSS 尺寸、文字层与坐标命中。
export const MAX_CANVAS_PIXELS_DESKTOP = 6_000_000;
export const MAX_CANVAS_PIXELS_MOBILE = 4_000_000;
export const MAX_CANVAS_DIMENSION = 4096;
const MOBILE_BREAKPOINT_PX = 768;

export function canvasRenderScale(
  pageWidth: number,
  pageHeight: number,
  cssScale: number,
  dpr: number,
  limits?: { maxPixels?: number; maxDimension?: number },
): number {
  const safeScale = Number.isFinite(cssScale) && cssScale > 0 ? cssScale : 1;
  const safeDpr = Math.min(Math.max(Number.isFinite(dpr) ? dpr : 1, 1), 2);
  const wanted = safeScale * safeDpr;
  if (!(pageWidth > 0 && pageHeight > 0)) return wanted;
  const maxPixels = limits?.maxPixels ?? MAX_CANVAS_PIXELS_DESKTOP;
  const maxDim = limits?.maxDimension ?? MAX_CANVAS_DIMENSION;
  const width = pageWidth * wanted;
  const height = pageHeight * wanted;
  const maxSide = Math.max(width, height);
  const dimFactor = maxSide > maxDim ? maxDim / maxSide : 1;
  const area = width * height;
  const areaFactor = area > maxPixels ? Math.sqrt(maxPixels / area) : 1;
  return wanted * Math.min(dimFactor, areaFactor);
}

interface PageUsage {
  refs: number;
  page: PDFPageProxy | null;
}

// 同一页可能同时挂载 PdfCanvas 与 PdfTextLayer，必须等两者全部退出且无在途任务后再 cleanup()，
// 避免画布绘制结束时提前释放正在被 TextLayer 使用的页面解码缓存或文本流。
const pageUsages = new WeakMap<PDFDocumentProxy, Map<number, PageUsage>>();

function acquirePageUsage(
  doc: PDFDocumentProxy,
  page: number,
): { attach: (proxy: PDFPageProxy) => void; release: () => void } {
  let map = pageUsages.get(doc);
  if (!map) {
    map = new Map<number, PageUsage>();
    pageUsages.set(doc, map);
  }
  let usage = map.get(page);
  if (!usage) {
    usage = { refs: 0, page: null };
    map.set(page, usage);
  }
  usage.refs += 1;
  let released = false;
  return {
    attach: (proxy) => {
      usage.page = proxy;
    },
    release: () => {
      if (released) return;
      released = true;
      usage.refs = Math.max(0, usage.refs - 1);
      if (usage.refs === 0) {
        map.delete(page);
        const proxy = usage.page as (PDFPageProxy & { cleanup?: () => void }) | null;
        usage.page = null;
        try {
          proxy?.cleanup?.();
        } catch {
          // 文档已销毁时忽略清理异常
        }
      }
    },
  };
}

function attachPageProxy(doc: PDFDocumentProxy, page: number, proxy: PDFPageProxy): void {
  const usage = pageUsages.get(doc)?.get(page);
  if (usage) usage.page = proxy;
}

interface PdfEntry {
  url: string;
  task: ReturnType<typeof pdfjs.getDocument>;
  promise: Promise<PDFDocumentProxy>;
  doc: PDFDocumentProxy | null;
  refs: number;
  lastUsed: number;
}

const entries = new Map<string, PdfEntry>();

function touch(entry: PdfEntry): void {
  entry.lastUsed = Date.now();
}

function createEntry(url: string): PdfEntry {
  const task = pdfjs.getDocument({
    url,
    useWasm: false,
    wasmUrl: DECODERS,
    disableAutoFetch: true,
    disableStream: true,
    rangeChunkSize: 256 * 1024,
  });
  const entry: PdfEntry = {
    url,
    task,
    promise: task.promise,
    doc: null,
    refs: 0,
    lastUsed: Date.now(),
  };
  entry.promise.then(
    (doc) => {
      entry.doc = doc;
      touch(entry);
      evict();
    },
    () => {
      if (entries.get(url) === entry) entries.delete(url);
    },
  );
  entries.set(url, entry);
  return entry;
}

function entryFor(url: string): PdfEntry {
  const existing = entries.get(url);
  if (existing) {
    touch(existing);
    return existing;
  }
  return createEntry(url);
}

function destroy(entry: PdfEntry): void {
  if (entry.doc) {
    const closable = entry.doc as PDFDocumentProxy & { destroy?: () => Promise<void> };
    if (typeof closable.destroy === "function") void closable.destroy().catch(() => undefined);
    else void entry.task.destroy().catch(() => undefined);
  } else {
    void entry.task.destroy().catch(() => undefined);
  }
}

function evict(): void {
  if (entries.size <= MAX_CACHED_PDFS) return;
  const candidates = [...entries.values()]
    .filter((entry) => entry.refs === 0)
    .sort((a, b) => a.lastUsed - b.lastUsed);
  while (entries.size > MAX_CACHED_PDFS) {
    const entry = candidates.shift();
    if (!entry) return; // 当前阅读中的文档全部保留
    if (entries.get(entry.url) !== entry || entry.refs > 0) continue;
    entries.delete(entry.url);
    destroy(entry);
  }
}

function acquire(url: string): { promise: Promise<PDFDocumentProxy>; release: () => void } {
  const entry = entryFor(url);
  entry.refs += 1;
  touch(entry);
  let released = false;
  return {
    promise: entry.promise,
    release: () => {
      if (released) return;
      released = true;
      entry.refs = Math.max(0, entry.refs - 1);
      touch(entry);
      evict();
    },
  };
}

export function loadPdf(url: string): Promise<PDFDocumentProxy> {
  return entryFor(url).promise;
}

export async function countPages(file: File): Promise<number> {
  const data = new Uint8Array(await file.arrayBuffer());
  const task = pdfjs.getDocument({ data, useWasm: false });
  try {
    return (await task.promise).numPages;
  } finally {
    void task.destroy();
  }
}

export function usePdf(url: string | null): { doc: PDFDocumentProxy | null; error: string | null } {
  const [state, setState] = useState<{ url: string; doc: PDFDocumentProxy | null; error: string | null } | null>(null);
  useEffect(() => {
    if (!url) return;
    let alive = true;
    const handle = acquire(url);
    handle.promise.then(
      (doc) => alive && setState({ url, doc, error: null }),
      (e: unknown) => alive && setState({ url, doc: null, error: String(e) }),
    );
    return () => {
      alive = false;
      handle.release();
    };
  }, [url]);
  return state?.url === url ? state : { doc: null, error: null };
}

interface CanvasProps {
  doc: PDFDocumentProxy;
  page: number; // 0 起
  scale: number; // CSS 像素 / pt
}

// 每个 canvas 上正在进行的渲染：pdf.js 的取消是异步的，旧渲染没停下时在同一 canvas 上开始新渲染会直接报错
const rendering = new WeakMap<HTMLCanvasElement, Promise<unknown>>();

/** 渲染一页；按设备像素比提高分辨率并受最大像素/边长预算约束，缩放变化或卸载时取消上一次渲染并释放显存。 */
export function PdfCanvas({ doc, page, scale }: CanvasProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const mountedRef = useRef(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    const canvas = ref.current;
    return () => {
      mountedRef.current = false;
      if (!canvas) return;
      const pending = rendering.get(canvas) ?? Promise.resolve();
      void pending
        .catch(() => undefined)
        .finally(() => {
          if (!mountedRef.current) {
            canvas.width = 0;
            canvas.height = 0;
          }
        });
    };
  }, []);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let task: ReturnType<pdfjs.PDFPageProxy["render"]> | null = null;
    let alive = true;
    const run = (rendering.get(canvas) ?? Promise.resolve())
      .catch(() => undefined)
      .then(() => (alive ? doc.getPage(page + 1) : null))
      .then((p) => {
        if (!alive || !p) return;
        attachPageProxy(doc, page, p);
        const base = p.getViewport({ scale: 1 });
        const maxPixels =
          typeof window !== "undefined" && window.innerWidth <= MOBILE_BREAKPOINT_PX
            ? MAX_CANVAS_PIXELS_MOBILE
            : MAX_CANVAS_PIXELS_DESKTOP;
        const renderScale = canvasRenderScale(base.width, base.height, scale, window.devicePixelRatio || 1, {
          maxPixels,
        });
        const viewport = p.getViewport({ scale: renderScale });
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        task = p.render({ canvas, viewport });
        return task.promise;
      })
      .then(
        () => alive && setError(null),
        (e: unknown) => {
          if (alive && !(e instanceof pdfjs.RenderingCancelledException)) setError(String(e));
        },
      );
    rendering.set(canvas, run);
    return () => {
      alive = false;
      task?.cancel();
    };
  }, [doc, page, scale]);

  // 页级引用放在 render effect 之后，确保卸载时先 cancel()，再在渲染链结算后释放 page 引用。
  useEffect(() => {
    const usage = acquirePageUsage(doc, page);
    const canvas = ref.current;
    return () => {
      const pending = (canvas && rendering.get(canvas)) || Promise.resolve();
      void pending
        .catch(() => undefined)
        .finally(() => {
          usage.release();
        });
    };
  }, [doc, page]);

  return (
    <>
      <canvas ref={ref} className="pdf-canvas" />
      {error && <p className="pdf-error">这一页没能画出来：{error}。请把浏览器更新到最新版后刷新。</p>}
    </>
  );
}

interface TextLayerProps extends CanvasProps {
  /** 需要词级高亮的原文术语（空字符串或 undefined 表示清除高亮） */
  highlight?: string;
  /** 文本流提取、渲染或缩放更新失败（非取消）时通知上层优雅降级 */
  onError?: () => void;
}

interface TextLayerState {
  layer: pdfjs.TextLayer;
  page: PDFPageProxy;
  ready: boolean;
}

const rawSpanText = new WeakMap<HTMLSpanElement, string>();

/**
 * 在 pdf.js TextLayer 的 span 集合内做无损词级高亮（支持单 span 及跨多 span 的术语匹配）。
 * 仅替换 span 内部子节点，不触碰 pdf.js 写入在 span 上的定位与缩放内联样式。
 */
export function highlightTextLayerSpans(container: HTMLElement, needle: string | undefined): void {
  const spans = [...container.querySelectorAll<HTMLSpanElement>("span")].filter(
    (span) => span.getAttribute("role") !== "img",
  );
  // 先无损还原此前高亮过的 span
  for (const span of spans) {
    const original = rawSpanText.get(span);
    if (original !== undefined) {
      span.textContent = original;
    } else {
      rawSpanText.set(span, span.textContent ?? "");
    }
  }

  const term = needle?.trim().toLowerCase();
  if (!term) return;

  const segments: Array<{ span: HTMLSpanElement; text: string; start: number; end: number }> = [];
  let joined = "";
  for (const span of spans) {
    const text = rawSpanText.get(span) ?? "";
    if (!text) continue;
    const start = joined.length;
    joined += text;
    segments.push({ span, text, start, end: joined.length });
  }
  if (!joined) return;

  const lower = joined.toLowerCase();
  const matches: Array<[number, number]> = [];
  let from = 0;
  while (from < lower.length) {
    const idx = lower.indexOf(term, from);
    if (idx === -1) break;
    matches.push([idx, idx + term.length]);
    from = idx + term.length;
  }
  if (matches.length === 0) return;

  for (const seg of segments) {
    const localRanges: Array<[number, number]> = [];
    for (const [mStart, mEnd] of matches) {
      if (mEnd <= seg.start || mStart >= seg.end) continue;
      localRanges.push([Math.max(mStart, seg.start) - seg.start, Math.min(mEnd, seg.end) - seg.start]);
    }
    if (localRanges.length === 0) continue;
    const frag = document.createDocumentFragment();
    let cursor = 0;
    for (const [s, e] of localRanges) {
      if (s > cursor) frag.append(document.createTextNode(seg.text.slice(cursor, s)));
      const mark = document.createElement("mark");
      mark.className = "term-highlight";
      mark.textContent = seg.text.slice(s, e);
      frag.append(mark);
      cursor = e;
    }
    if (cursor < seg.text.length) frag.append(document.createTextNode(seg.text.slice(cursor)));
    seg.span.replaceChildren(frag);
  }
}

/**
 * 渲染原生 PDF 页的 pdf.js TextLayer（逐字与画布对齐，支持精准划选、复制、Ctrl+F 与词级术语高亮）。
 * 仅 scale 变化时复用已提取文本流并走 layer.update()，不重复请求 worker；翻页/卸载时 cancel()，真实失败时清理并回调 onError。
 */
export function PdfTextLayer({ doc, page, scale, highlight, onError }: TextLayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef<TextLayerState | null>(null);
  const scaleRef = useRef(scale);
  const highlightRef = useRef(highlight);
  const onErrorRef = useRef(onError);

  useEffect(() => {
    highlightRef.current = highlight;
    onErrorRef.current = onError;
  }, [highlight, onError]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let alive = true;
    let localState: TextLayerState | null = null;
    const usage = acquirePageUsage(doc, page);
    container.replaceChildren();

    const failAndCleanup = () => {
      if (localState) {
        if (stateRef.current === localState) stateRef.current = null;
        try {
          localState.layer.cancel();
        } catch {
          // 忽略取消时的二次异常
        }
      }
      container.replaceChildren();
      onErrorRef.current?.();
    };

    const run = doc
      .getPage(page + 1)
      .then((p) => {
        if (!alive) return;
        usage.attach(p);
        const initialScale = scaleRef.current;
        container.style.setProperty("--total-scale-factor", String(initialScale));
        const viewport = p.getViewport({ scale: initialScale });
        const layer = new pdfjs.TextLayer({
          textContentSource: p.streamTextContent(),
          container,
          viewport,
        });
        localState = { layer, page: p, ready: false };
        stateRef.current = localState;
        return layer.render().then(() => {
          if (!alive || !localState || stateRef.current !== localState) return;
          localState.ready = true;
          // 文本流加载期间若缩放已变，立即追平到最新缩放比例
          if (scaleRef.current !== initialScale) {
            container.style.setProperty("--total-scale-factor", String(scaleRef.current));
            localState.layer.update({ viewport: p.getViewport({ scale: scaleRef.current }) });
          }
          highlightTextLayerSpans(container, highlightRef.current);
        });
      })
      .catch((e: unknown) => {
        if (!alive || e instanceof pdfjs.AbortException) return;
        failAndCleanup();
      });

    return () => {
      alive = false;
      if (localState) {
        if (stateRef.current === localState) stateRef.current = null;
        try {
          localState.layer.cancel();
        } catch {
          // 忽略已结束任务的取消异常
        }
      }
      container.replaceChildren();
      // 等 getPage / TextLayer 渲染与取消链彻底结算后再释放页引用，避免提前触发 page.cleanup()
      void run.finally(() => {
        usage.release();
      });
    };
  }, [doc, page]);

  useEffect(() => {
    scaleRef.current = scale;
    const container = containerRef.current;
    if (!container) return;
    container.style.setProperty("--total-scale-factor", String(scale));
    const current = stateRef.current;
    if (current?.ready) {
      try {
        current.layer.update({ viewport: current.page.getViewport({ scale }) });
      } catch {
        stateRef.current = null;
        try {
          current.layer.cancel();
        } catch {
          // 忽略取消异常
        }
        container.replaceChildren();
        onErrorRef.current?.();
      }
    }
  }, [scale]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !stateRef.current?.ready) return;
    highlightTextLayerSpans(container, highlight);
  }, [highlight]);

  return (
    <div
      ref={containerRef}
      className="pdf-text-layer textLayer"
      style={{ "--total-scale-factor": scale } as CSSProperties}
    />
  );
}
