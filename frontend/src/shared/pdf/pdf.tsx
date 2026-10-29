/**
 * pdf.js 封装：加载文档（同一地址只加载一次）、渲染单页到 canvas、统计本地文件页数。
 * useWasm=false：WebAssembly 解码需要放宽 CSP，改用纯 JS 解码器。扫描件常用的 CCITT 传真、JBIG2、JPEG 2000 图片
 * 的解码器按 wasmUrl 加载（构建时原名放在 /pdfjs/，见 vite.config.ts），不给这个地址扫描页会画成空白。
 * 用 legacy 构建：默认构建直接调用 Promise.try、Map.getOrInsertComputed 等 2025 年后才有的 API，
 * 稍旧的 Chrome 上整页画不出来（原文页空白、译文页没有图片），legacy 构建自带补丁。
 */
import * as pdfjs from "pdfjs-dist/legacy/build/pdf.mjs";
import type { PDFDocumentProxy } from "pdfjs-dist/legacy/build/pdf.mjs";
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const docs = new Map<string, Promise<PDFDocumentProxy>>();
const DECODERS = "/pdfjs/";

export function loadPdf(url: string): Promise<PDFDocumentProxy> {
  let p = docs.get(url);
  if (!p) {
    p = pdfjs.getDocument({ url, useWasm: false, wasmUrl: DECODERS }).promise;
    p.catch(() => docs.delete(url));
    docs.set(url, p);
  }
  return p;
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
  const [state, setState] = useState<{ doc: PDFDocumentProxy | null; error: string | null }>({ doc: null, error: null });
  useEffect(() => {
    if (!url) return;
    let alive = true;
    loadPdf(url).then(
      (doc) => alive && setState({ doc, error: null }),
      (e: unknown) => alive && setState({ doc: null, error: String(e) }),
    );
    return () => {
      alive = false;
    };
  }, [url]);
  return state;
}

interface CanvasProps {
  doc: PDFDocumentProxy;
  page: number; // 0 起
  scale: number; // CSS 像素 / pt
}

// 每个 canvas 上正在进行的渲染：pdf.js 的取消是异步的，旧渲染没停下时在同一 canvas 上开始新渲染会直接报错
const rendering = new WeakMap<HTMLCanvasElement, Promise<unknown>>();

/** 渲染一页；按设备像素比提高分辨率，缩放变化或卸载时取消上一次渲染。失败时显示原因，不留空白。 */
export function PdfCanvas({ doc, page, scale }: CanvasProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState<string | null>(null);
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
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const viewport = p.getViewport({ scale: scale * ratio });
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
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
  return (
    <>
      <canvas ref={ref} className="pdf-canvas" />
      {error && <p className="pdf-error">这一页没能画出来：{error}。请把浏览器更新到最新版后刷新。</p>}
    </>
  );
}
