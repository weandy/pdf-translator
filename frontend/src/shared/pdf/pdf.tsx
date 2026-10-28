/**
 * pdf.js 封装：加载文档（同一地址只加载一次）、渲染单页到 canvas、统计本地文件页数。
 * useWasm=false：WebAssembly 解码需要额外部署 wasm 文件并放宽 CSP，pdf.js 会退回 JS 实现。
 */
import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

const docs = new Map<string, Promise<PDFDocumentProxy>>();

export function loadPdf(url: string): Promise<PDFDocumentProxy> {
  let p = docs.get(url);
  if (!p) {
    p = pdfjs.getDocument({ url, useWasm: false }).promise;
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

/** 渲染一页；按设备像素比提高分辨率，缩放变化或卸载时取消上一次渲染。 */
export function PdfCanvas({ doc, page, scale }: CanvasProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let task: ReturnType<pdfjs.PDFPageProxy["render"]> | null = null;
    let alive = true;
    void doc.getPage(page + 1).then((p) => {
      const canvas = ref.current;
      if (!alive || !canvas) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const viewport = p.getViewport({ scale: scale * ratio });
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      task = p.render({ canvas, viewport });
      task.promise.catch(() => undefined); // 取消渲染时会 reject，忽略
    });
    return () => {
      alive = false;
      task?.cancel();
    };
  }, [doc, page, scale]);
  return <canvas ref={ref} className="pdf-canvas" />;
}
