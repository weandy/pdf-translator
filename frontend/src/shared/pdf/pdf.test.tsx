import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { PDFDocumentProxy } from "pdfjs-dist/legacy/build/pdf.mjs";

const textLayerMocks = vi.hoisted(() => {
  const instances: Array<{
    container: HTMLElement;
    viewport: { scale: number };
    render: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
    resolveRender: () => void;
  }> = [];

  class MockTextLayer {
    container: HTMLElement;
    viewport: { scale: number };
    render: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
    resolveRender: () => void;

    constructor(opts: { container: HTMLElement; viewport: { scale: number } }) {
      this.container = opts.container;
      this.viewport = opts.viewport;
      let done = () => {};
      const promise = new Promise<void>((resolve) => {
        done = resolve;
      });
      this.resolveRender = done;
      this.render = vi.fn(() => {
        const span = document.createElement("span");
        span.textContent = "PDF text";
        this.container.appendChild(span);
        return promise;
      });
      this.update = vi.fn(({ viewport }: { viewport: { scale: number } }) => {
        this.viewport = viewport;
      });
      this.cancel = vi.fn();
      instances.push(this);
    }
  }

  return { instances, MockTextLayer };
});

vi.mock("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url", () => ({ default: "/mock-worker.mjs" }));

vi.mock("pdfjs-dist/legacy/build/pdf.mjs", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  AbortException: class AbortException extends Error {},
  RenderingCancelledException: class RenderingCancelledException extends Error {},
  TextLayer: textLayerMocks.MockTextLayer,
  getDocument: vi.fn(),
}));

import { PdfTextLayer } from "@/shared/pdf/pdf";

function createFakeDoc() {
  const streamTextContent = vi.fn(() => ({}) as ReadableStream);
  const getViewport = vi.fn(({ scale }: { scale: number }) => ({ scale, width: 600 * scale, height: 800 * scale }));
  const pageObj = { streamTextContent, getViewport };
  const getPage = vi.fn(async () => pageObj);
  return {
    doc: { getPage } as unknown as PDFDocumentProxy,
    getPage,
    streamTextContent,
    getViewport,
  };
}

describe("PdfTextLayer", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  beforeEach(() => {
    textLayerMocks.instances.length = 0;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  it("首次挂载渲染 pdf.js TextLayer，仅 scale 变化时走 update 不重建文本流", async () => {
    const fake = createFakeDoc();
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1.25} />);
      await Promise.resolve();
    });

    expect(fake.getPage).toHaveBeenCalledTimes(1);
    expect(fake.getPage).toHaveBeenCalledWith(1);
    expect(fake.streamTextContent).toHaveBeenCalledTimes(1);
    expect(textLayerMocks.instances).toHaveLength(1);
    const layer = textLayerMocks.instances[0]!;
    expect(layer.render).toHaveBeenCalledTimes(1);
    expect(layer.container.style.getPropertyValue("--total-scale-factor")).toBe("1.25");

    // 完成首次 render
    await act(async () => {
      layer.resolveRender();
      await Promise.resolve();
    });

    // 仅缩放变化：应走 update 而不是重新 streamTextContent 或新建 TextLayer
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1.75} />);
      await Promise.resolve();
    });

    expect(fake.streamTextContent).toHaveBeenCalledTimes(1);
    expect(textLayerMocks.instances).toHaveLength(1);
    expect(layer.update).toHaveBeenCalledTimes(1);
    expect(layer.viewport.scale).toBe(1.75);
    expect(layer.container.style.getPropertyValue("--total-scale-factor")).toBe("1.75");
  });

  it("切换页或卸载时取消正在进行的 TextLayer 并清空旧节点", async () => {
    const fake = createFakeDoc();
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} />);
      await Promise.resolve();
    });
    const first = textLayerMocks.instances[0]!;
    expect(first.container.children.length).toBeGreaterThan(0);

    // 切换到第 2 页
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={1} scale={1} />);
      await Promise.resolve();
    });
    expect(first.cancel).toHaveBeenCalledTimes(1);
    expect(textLayerMocks.instances).toHaveLength(2);
    expect(fake.getPage).toHaveBeenLastCalledWith(2);

    // 卸载
    const second = textLayerMocks.instances[1]!;
    await act(async () => root.unmount());
    expect(second.cancel).toHaveBeenCalledTimes(1);
  });

  it("文本流渲染中途发生缩放时，渲染完成后自动追平到最新缩放比例", async () => {
    const fake = createFakeDoc();
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} />);
      await Promise.resolve();
    });
    const layer = textLayerMocks.instances[0]!;
    expect(layer.update).not.toHaveBeenCalled();

    // 渲染尚未完成时先触发缩放
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={2} />);
      await Promise.resolve();
    });
    expect(layer.update).not.toHaveBeenCalled();

    // 首次 render 完成后应立即补一次 update(scale=2)
    await act(async () => {
      layer.resolveRender();
      await Promise.resolve();
    });
    expect(layer.update).toHaveBeenCalledTimes(1);
    expect(layer.viewport.scale).toBe(2);
  });
});
