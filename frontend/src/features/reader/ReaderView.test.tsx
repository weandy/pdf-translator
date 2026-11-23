import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ReaderView } from "@/features/reader/ReaderView";
import type { DocumentData } from "@/shared/api/client";

const pdfMock = vi.hoisted(() => ({
  originalDoc: null as unknown,
  textLayerCalls: [] as Array<{ page: number; scale: number }>,
}));

vi.mock("@/shared/pdf/pdf", () => ({
  usePdf: (url: string | null) => ({
    doc: url && url.includes("original.pdf") ? pdfMock.originalDoc : null,
    error: null,
  }),
  PdfCanvas: () => null,
  PdfTextLayer: ({ page, scale }: { page: number; scale: number }) => {
    pdfMock.textLayerCalls.push({ page, scale });
    return <div className="pdf-text-layer textLayer" data-testid={`pdf-text-layer-${page}`} />;
  },
}));

function makeDoc(pageCount = 3): DocumentData {
  const pages = Array.from({ length: pageCount }, (_, index) => ({
    index,
    width: 600,
    height: 800,
    kind: "native" as const,
    blocks:
      index === 0 || index === pageCount - 1 || index === 79
        ? [
            {
              id: `b-${index}`,
              page: index,
              type: "text" as const,
              bbox: { x0: 20, y0: 100, x1: 200, y1: 140 },
              font_size: 12,
              color: 0,
              bold: false,
              italic: false,
              serif: true,
              translatable: true,
            },
          ]
        : [],
  }));
  const segments = pages.flatMap((p, i) => {
    const block = p.blocks[0];
    if (!block) return [];
    return [
      {
        id: i === 0 ? "s1" : `s-${p.index}`,
        block_ids: [block.id],
        parts: [{ block_id: block.id, chars: 10 }],
        source: p.index === 79 ? "Far away target keyword" : `Synthetic paragraph ${p.index}`,
        target: p.index === 79 ? "远端命中译文" : null,
        status: p.index === 79 ? ("done" as const) : ("failed" as const),
        quality: "default" as const,
        formulas: {},
        translation_attempts: 0,
        failure_reason: "",
      },
    ];
  });
  return {
    job: {} as DocumentData["job"],
    title: "合成文档",
    links: [],
    outline: [],
    pages,
    segments,
  };
}

const doc = makeDoc(3);

function setInputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("阅读位置恢复、手动段落定位与虚拟化搜索", () => {
  let container: HTMLDivElement;
  let root: Root;
  const scrollTo = vi.fn();
  const ioSpy = vi.fn();

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.setItem("pdftr.pos.v1", JSON.stringify({ synthetic: 2 }));
    scrollTo.mockClear();
    ioSpy.mockClear();
    pdfMock.originalDoc = null;
    pdfMock.textLayerCalls.length = 0;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor() {
          ioSpy();
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => setTimeout(() => callback(0), 16));
    vi.stubGlobal("cancelAnimationFrame", (handle: number) => clearTimeout(handle));
    Object.defineProperty(Element.prototype, "scrollTo", { configurable: true, value: scrollTo });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    Reflect.deleteProperty(Element.prototype, "scrollTo");
    localStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function render(
    spot: { id: string; n: number } | null = null,
    targetDoc: DocumentData = doc,
    jobId = "synthetic",
    onSelect: (id: string) => void = () => {},
  ) {
    await act(async () =>
      root.render(
        <ReaderView doc={targetDoc} jobId={jobId} selectedSegment={null} onSelectSegment={onSelect} spot={spot} />,
      ),
    );
  }

  it("未操作时恢复上次阅读位置到第 2 页精确坐标", async () => {
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(450));
    expect(scrollTo).toHaveBeenCalledTimes(1);
    const arg = scrollTo.mock.calls[0]?.[0] as { top: number; behavior: string } | undefined;
    expect(arg).toEqual({ top: expect.any(Number), behavior: "auto" });
    expect(arg?.top).toBeGreaterThan(0);
  });

  it("手动定位取消待执行的自动恢复，重复定位仍能生效", async () => {
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(100));
    await render({ id: "s1", n: 1 });
    await act(async () => vi.advanceTimersByTimeAsync(450));
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect(scrollTo).toHaveBeenLastCalledWith({ top: expect.any(Number), behavior: "smooth" });
    await render({ id: "s1", n: 2 });
    await act(async () => vi.advanceTimersByTimeAsync(40));
    expect(scrollTo).toHaveBeenCalledTimes(2);
  });

  it("按实际页宽定位，侧栏或滚动条改变布局后不使用过期缩放比例", async () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      return this.classList.contains("page") ? new DOMRect(0, 0, 600, 800) : new DOMRect();
    });
    await render({ id: "s1", n: 1 });
    await act(async () => vi.advanceTimersByTimeAsync(40));
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 44, behavior: "smooth" });
  });

  it("100 页长文档仅挂载视口与缓冲窗口内页面且不创建 IntersectionObserver", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc");
    const rows = container.querySelectorAll(".page-row");
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(12);
    expect(ioSpy).not.toHaveBeenCalled();
  });

  it("滚动长文档时切换挂载窗口并更新当前页码", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-scroll");
    const scroller = container.querySelector<HTMLDivElement>(".pages");
    expect(scroller).not.toBeNull();
    // 滚到文档中段（约第 50 页附近）
    const total = Number.parseFloat(container.querySelector<HTMLDivElement>(".pages-canvas")?.style.height ?? "0");
    expect(total).toBeGreaterThan(20000);
    scroller!.scrollTop = total / 2;
    await act(async () => {
      scroller!.dispatchEvent(new Event("scroll"));
      await vi.advanceTimersByTimeAsync(32);
    });
    expect(container.querySelector('.page-row[data-page="0"]')).toBeNull();
    const mountedPages = [...container.querySelectorAll<HTMLElement>(".page-row")].map((el) => Number(el.dataset.page));
    expect(mountedPages.some((p) => p >= 45 && p <= 55)).toBe(true);
    const pageInput = container.querySelector<HTMLInputElement>('input[aria-label="跳到第几页"]');
    expect(Number(pageInput?.value)).toBeGreaterThanOrEqual(45);
  });

  it("搜索防抖 150ms 后出结果，并可跳转到尚未挂载的第 80 页", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-search");
    expect(container.querySelector('.page-row[data-page="79"]')).toBeNull();

    const searchBtn = container.querySelector<HTMLButtonElement>('button[title="搜索原文和译文"]');
    await act(async () => searchBtn?.click());
    const input = container.querySelector<HTMLInputElement>('input[aria-label="搜索原文或译文"]');
    expect(input).not.toBeNull();

    await act(async () => setInputValue(input!, "远端命中译文"));
    // 未到 150ms 防抖时间时结果列表尚未出现
    expect(container.querySelectorAll(".search-result").length).toBe(0);

    await act(async () => vi.advanceTimersByTimeAsync(160));
    const results = container.querySelectorAll<HTMLButtonElement>(".search-result");
    expect(results.length).toBe(1);

    await act(async () => results[0]?.click());
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect((scrollTo.mock.calls[0]?.[0] as { top: number } | undefined)?.top).toBeGreaterThan(10000);
    // 跳转后目标页已纳入窗口挂载
    expect(container.querySelector('.page-row[data-page="79"]')).not.toBeNull();
  });

  it("搜索输入框按 Enter 立即生效无需等待防抖", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-enter");
    const searchBtn = container.querySelector<HTMLButtonElement>('button[title="搜索原文和译文"]');
    await act(async () => searchBtn?.click());
    const input = container.querySelector<HTMLInputElement>('input[aria-label="搜索原文或译文"]');

    await act(async () => {
      setInputValue(input!, "Far away target");
      input!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(scrollTo).toHaveBeenCalledTimes(1);
    expect((scrollTo.mock.calls[0]?.[0] as { top: number } | undefined)?.top).toBeGreaterThan(10000);
  });

  it("原生页渲染 PdfTextLayer，扫描页降级保留块级 .source-text-layer", async () => {
    pdfMock.originalDoc = { fake: true };
    const mixedDoc = makeDoc(2);
    mixedDoc.pages[1]!.kind = "scanned";
    await render(null, mixedDoc, "mixed-doc");

    const nativePage = container.querySelector('.page-row[data-page="0"] .page[aria-label="原文第 1 页"]');
    const scannedPage = container.querySelector('.page-row[data-page="1"] .page[aria-label="原文第 2 页"]');
    expect(nativePage?.querySelector('[data-testid="pdf-text-layer-0"]')).not.toBeNull();
    expect(nativePage?.querySelector(".source-text-layer")).toBeNull();
    expect(scannedPage?.querySelector('[data-testid="pdf-text-layer-1"]')).toBeNull();
    expect(scannedPage?.querySelector(".source-text-layer")).not.toBeNull();
  });

  it("原文页通过坐标命中触发点击选中与悬停联动，且划词选区非空时不误触点击", async () => {
    pdfMock.originalDoc = { fake: true };
    const onSelect = vi.fn();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      return this.classList.contains("page") ? new DOMRect(100, 50, 600, 800) : new DOMRect();
    });
    await render(null, doc, "hit-doc", onSelect);

    const origPage = container.querySelector<HTMLElement>('.page-row[data-page="0"] .page[aria-label="原文第 1 页"]');
    expect(origPage).not.toBeNull();
    // b-0 bbox: x=[20, 200], y=[100, 140], page rect=(100, 50, 600, 800) -> shownScale = 1
    // 命中点：(100 + 50, 50 + 120) = (150, 170)
    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(container.querySelector(".hit.hovered")).not.toBeNull();
    expect(container.querySelector(".tblock.hovered")).not.toBeNull();

    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("s1");

    // 划词复制期间（selection 非空）不触发段落选中
    const getSelectionSpy = vi
      .spyOn(window, "getSelection")
      .mockReturnValue({ toString: () => "Synthetic" } as unknown as Selection);
    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    getSelectionSpy.mockRestore();

    // 点击空白处不触发选中，鼠标移到空白处与移出页面均清除 hovered
    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 500, clientY: 500 }));
      origPage!.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 500, clientY: 500 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(container.querySelector(".hit.hovered")).toBeNull();

    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(container.querySelector(".hit.hovered")).not.toBeNull();
    await act(async () => {
      origPage!.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }));
    });
    expect(container.querySelector(".hit.hovered")).toBeNull();
  });
});
