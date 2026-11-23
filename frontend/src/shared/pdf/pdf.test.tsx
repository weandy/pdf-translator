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
    rejectRender: (err: Error) => void;
  }> = [];

  let updateThrows = false;
  let spanTexts = ["PDF text"];

  class MockTextLayer {
    container: HTMLElement;
    viewport: { scale: number };
    render: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
    resolveRender: () => void;
    rejectRender: (err: Error) => void;

    constructor(opts: { container: HTMLElement; viewport: { scale: number } }) {
      this.container = opts.container;
      this.viewport = opts.viewport;
      let done = () => {};
      let fail: (err: Error) => void = () => {};
      const promise = new Promise<void>((resolve, reject) => {
        done = resolve;
        fail = reject;
      });
      this.resolveRender = done;
      this.rejectRender = fail;
      this.render = vi.fn(() => {
        for (const text of spanTexts) {
          const span = document.createElement("span");
          span.textContent = text;
          this.container.appendChild(span);
        }
        return promise;
      });
      this.update = vi.fn(({ viewport }: { viewport: { scale: number } }) => {
        if (updateThrows) throw new Error("update boom");
        this.viewport = viewport;
      });
      this.cancel = vi.fn();
      instances.push(this);
    }
  }

  return {
    instances,
    MockTextLayer,
    setUpdateThrows: (v: boolean) => {
      updateThrows = v;
    },
    setSpanTexts: (texts: string[]) => {
      spanTexts = texts;
    },
  };
});

vi.mock("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url", () => ({ default: "/mock-worker.mjs" }));

vi.mock("pdfjs-dist/legacy/build/pdf.mjs", () => ({
  GlobalWorkerOptions: { workerSrc: "" },
  AbortException: class AbortException extends Error {},
  RenderingCancelledException: class RenderingCancelledException extends Error {},
  TextLayer: textLayerMocks.MockTextLayer,
  getDocument: vi.fn(),
}));

import {
  canvasRenderScale,
  highlightTextLayerSpans,
  MAX_CANVAS_DIMENSION,
  MAX_CANVAS_PIXELS_DESKTOP,
  MAX_CANVAS_PIXELS_MOBILE,
  PdfCanvas,
  PdfTextLayer,
} from "@/shared/pdf/pdf";

function createFakeDoc(
  rejectGetPage?: Error,
  pageSize: { width: number; height: number } = { width: 600, height: 800 },
) {
  const streamTextContent = vi.fn(() => ({}) as ReadableStream);
  const cleanup = vi.fn();
  const renderCalls: Array<{
    cancel: ReturnType<typeof vi.fn>;
    resolve: () => void;
    reject: (err: Error) => void;
    viewport: { scale: number; width: number; height: number };
  }> = [];
  const getViewport = vi.fn(({ scale }: { scale: number }) => ({
    scale,
    width: pageSize.width * scale,
    height: pageSize.height * scale,
  }));
  const render = vi.fn(({ viewport }: { canvas: HTMLCanvasElement; viewport: { scale: number; width: number; height: number } }) => {
    let resolve = () => {};
    let reject: (err: Error) => void = () => {};
    const promise = new Promise<void>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    const cancel = vi.fn();
    renderCalls.push({ cancel, resolve, reject, viewport });
    return { promise, cancel };
  });
  const pageObj = { streamTextContent, getViewport, render, cleanup };
  const getPage = vi.fn(async () => {
    if (rejectGetPage) throw rejectGetPage;
    return pageObj;
  });
  return {
    doc: { getPage } as unknown as PDFDocumentProxy,
    getPage,
    streamTextContent,
    getViewport,
    render,
    renderCalls,
    cleanup,
  };
}

describe("PdfTextLayer 与词级术语高亮", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  beforeEach(() => {
    textLayerMocks.instances.length = 0;
    textLayerMocks.setUpdateThrows(false);
    textLayerMocks.setSpanTexts(["PDF text"]);
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

    await act(async () => {
      layer.resolveRender();
      await Promise.resolve();
    });

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

    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={1} scale={1} />);
      await Promise.resolve();
    });
    expect(first.cancel).toHaveBeenCalledTimes(1);
    expect(textLayerMocks.instances).toHaveLength(2);
    expect(fake.getPage).toHaveBeenLastCalledWith(2);

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

    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={2} />);
      await Promise.resolve();
    });
    expect(layer.update).not.toHaveBeenCalled();

    await act(async () => {
      layer.resolveRender();
      await Promise.resolve();
    });
    expect(layer.update).toHaveBeenCalledTimes(1);
    expect(layer.viewport.scale).toBe(2);
  });

  it("支持跨 span 词级术语高亮与清空无损还原", async () => {
    textLayerMocks.setSpanTexts(["Self-", "Attention ", "Mechanism"]);
    const fake = createFakeDoc();
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} highlight="attention mechanism" />);
      await Promise.resolve();
    });
    const layer = textLayerMocks.instances[0]!;
    await act(async () => {
      layer.resolveRender();
      await Promise.resolve();
    });

    const marks = [...layer.container.querySelectorAll("mark.term-highlight")].map((m) => m.textContent);
    expect(marks).toEqual(["Attention ", "Mechanism"]);

    // 清空高亮后无损还原原文本
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} highlight={undefined} />);
      await Promise.resolve();
    });
    expect(layer.container.querySelectorAll("mark.term-highlight")).toHaveLength(0);
    expect(layer.container.textContent).toBe("Self-Attention Mechanism");
  });

  it("highlightTextLayerSpans 跳过 img 角色 span 并保留 span 内联样式", () => {
    const host = document.createElement("div");
    const imgSpan = document.createElement("span");
    imgSpan.setAttribute("role", "img");
    imgSpan.textContent = "Attention";
    const textSpan = document.createElement("span");
    textSpan.style.left = "12px";
    textSpan.textContent = "Use Attention now";
    host.append(imgSpan, textSpan);

    highlightTextLayerSpans(host, "attention");
    expect(imgSpan.querySelector("mark")).toBeNull();
    expect(textSpan.querySelector("mark.term-highlight")?.textContent).toBe("Attention");
    expect(textSpan.style.left).toBe("12px");
  });

  it("文本流渲染或缩放 update 出现非取消异常时清理残留 DOM 并回调 onError", async () => {
    const onError = vi.fn();
    const fake = createFakeDoc();
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} onError={onError} />);
      await Promise.resolve();
    });
    const layer = textLayerMocks.instances[0]!;
    expect(layer.container.children.length).toBeGreaterThan(0);

    await act(async () => {
      layer.rejectRender(new Error("corrupt stream"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(onError).toHaveBeenCalledTimes(1);
    expect(layer.cancel).toHaveBeenCalledTimes(1);
    expect(layer.container.children.length).toBe(0);

    // 另一页测试 update() 抛出异常时也清理并触发 onError
    const onErrorUpdate = vi.fn();
    textLayerMocks.setUpdateThrows(true);
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={1} scale={1} onError={onErrorUpdate} />);
      await Promise.resolve();
    });
    const layer2 = textLayerMocks.instances[1]!;
    await act(async () => {
      layer2.resolveRender();
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={1} scale={1.5} onError={onErrorUpdate} />);
      await Promise.resolve();
    });
    expect(onErrorUpdate).toHaveBeenCalledTimes(1);
    expect(layer2.cancel).toHaveBeenCalledTimes(1);
    expect(layer2.container.children.length).toBe(0);
  });

  it("getPage 拒绝时清理并通知 onError", async () => {
    const onError = vi.fn();
    const fake = createFakeDoc(new Error("page load failed"));
    await act(async () => {
      root.render(<PdfTextLayer doc={fake.doc} page={0} scale={1} onError={onError} />);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(onError).toHaveBeenCalledTimes(1);
  });
});

describe("PdfCanvas 像素预算、卸载归零与页级 cleanup 协调", () => {
  let container: HTMLDivElement;
  let root: Root;
  const originalDpr = window.devicePixelRatio;
  const originalInnerWidth = window.innerWidth;

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  beforeEach(() => {
    textLayerMocks.instances.length = 0;
    textLayerMocks.setUpdateThrows(false);
    textLayerMocks.setSpanTexts(["PDF text"]);
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 2 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: originalDpr });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: originalInnerWidth });
    vi.clearAllMocks();
  });

  it("canvasRenderScale 对正常缩放不降质，对高缩放与超长边按像素和 4096 上限封顶", () => {
    // 常规 A4 1x + dpr=2：1190×1684 ≈ 2.0M px，远低于桌面 6M 上限，保持全精度
    expect(canvasRenderScale(595, 842, 1, 2, { maxPixels: MAX_CANVAS_PIXELS_DESKTOP })).toBeCloseTo(2, 5);

    // A4 4x + dpr=2：原需 4760×6736 ≈ 32M px，应被压到 <= 6M px 且单边 <= 4096
    const cappedDesktop = canvasRenderScale(595, 842, 4, 2, { maxPixels: MAX_CANVAS_PIXELS_DESKTOP });
    expect(595 * cappedDesktop * (842 * cappedDesktop)).toBeLessThanOrEqual(MAX_CANVAS_PIXELS_DESKTOP + 1);
    expect(842 * cappedDesktop).toBeLessThanOrEqual(MAX_CANVAS_DIMENSION + 0.01);

    // 移动端上限更严（<= 4M px）
    const cappedMobile = canvasRenderScale(595, 842, 4, 2, { maxPixels: MAX_CANVAS_PIXELS_MOBILE });
    expect(595 * cappedMobile * (842 * cappedMobile)).toBeLessThanOrEqual(MAX_CANVAS_PIXELS_MOBILE + 1);
    expect(cappedMobile).toBeLessThan(cappedDesktop);

    // 超细长海报页（400×3000 pt，面积未必超但单边超 4096）受单边上限约束
    const cappedStrip = canvasRenderScale(400, 3000, 1, 2, { maxPixels: MAX_CANVAS_PIXELS_DESKTOP });
    expect(3000 * cappedStrip).toBeLessThanOrEqual(MAX_CANVAS_DIMENSION + 0.01);
  });

  it("PdfCanvas 在高缩放下限制画布像素，卸载且渲染结算后归零 canvas 宽高释放显存", async () => {
    const fake = createFakeDoc(undefined, { width: 595, height: 842 });
    await act(async () => {
      root.render(<PdfCanvas doc={fake.doc} page={0} scale={4} />);
      await Promise.resolve();
      await Promise.resolve();
    });

    const canvas = container.querySelector("canvas")!;
    expect(canvas.width * canvas.height).toBeLessThanOrEqual(MAX_CANVAS_PIXELS_DESKTOP);
    expect(Math.max(canvas.width, canvas.height)).toBeLessThanOrEqual(MAX_CANVAS_DIMENSION);
    expect(canvas.width).toBeGreaterThan(0);

    const task = fake.renderCalls[0]!;
    await act(async () => {
      root.unmount();
      await Promise.resolve();
    });
    expect(task.cancel).toHaveBeenCalledTimes(1);

    await act(async () => {
      task.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(canvas.width).toBe(0);
    expect(canvas.height).toBe(0);
    expect(fake.cleanup).toHaveBeenCalledTimes(1);
  });

  it("同一页同时挂载 PdfCanvas 与 PdfTextLayer 时，仅在两者都卸载且任务结束后才调用 page.cleanup", async () => {
    const fake = createFakeDoc();
    await act(async () => {
      root.render(
        <>
          <PdfCanvas doc={fake.doc} page={0} scale={1} />
          <PdfTextLayer doc={fake.doc} page={0} scale={1} />
        </>,
      );
      await new Promise((r) => setTimeout(r, 0));
    });

    const canvasTask = fake.renderCalls[0]!;
    const textLayer = textLayerMocks.instances[0]!;

    // 画布先渲染完成，文字层仍在运行：绝不能提前 cleanup
    await act(async () => {
      canvasTask.resolve();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(fake.cleanup).not.toHaveBeenCalled();

    // 仅卸载 PdfCanvas、保持同一个 PdfTextLayer 挂载：仍不 cleanup
    await act(async () => {
      root.render(
        <>
          {null}
          <PdfTextLayer doc={fake.doc} page={0} scale={1} />
        </>,
      );
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(textLayerMocks.instances).toHaveLength(1);
    expect(fake.cleanup).not.toHaveBeenCalled();

    // 卸载 PdfTextLayer 时，若 TextLayer.render() 尚未结算，即使刷空全部微任务也绝不提前 cleanup
    await act(async () => {
      root.unmount();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(textLayer.cancel).toHaveBeenCalledTimes(1);
    expect(fake.cleanup).not.toHaveBeenCalled();

    // 等 TextLayer.render() 结算后，引用归零才触发一次 page.cleanup
    await act(async () => {
      textLayer.resolveRender();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(fake.cleanup).toHaveBeenCalledTimes(1);
  });
});
