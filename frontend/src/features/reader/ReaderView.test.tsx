import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ReaderView } from "@/features/reader/ReaderView";
import type { DocumentData } from "@/shared/api/client";

const pdfMock = vi.hoisted(() => ({
  originalDoc: null as unknown,
  textLayerCalls: [] as Array<{ page: number; scale: number; highlight?: string }>,
  triggerErrorOnPage: null as number | null,
}));

vi.mock("@/shared/pdf/pdf", () => ({
  usePdf: (url: string | null) => ({
    doc: url && url.includes("original.pdf") ? pdfMock.originalDoc : null,
    error: null,
  }),
  PdfCanvas: () => null,
  PdfTextLayer: ({
    page,
    scale,
    highlight,
    onError,
  }: {
    page: number;
    scale: number;
    highlight?: string;
    onError?: () => void;
  }) => {
    pdfMock.textLayerCalls.push({ page, scale, highlight });
    if (pdfMock.triggerErrorOnPage === page) {
      queueMicrotask(() => onError?.());
    }
    return (
      <div className="pdf-text-layer textLayer" data-testid={`pdf-text-layer-${page}`}>
        {highlight ? <mark className="term-highlight">{highlight}</mark> : null}
      </div>
    );
  },
}));

function makeDoc(pageCount = 3, customIndices?: number[]): DocumentData {
  const indices = customIndices ?? Array.from({ length: pageCount }, (_, i) => i);
  const pages = indices.map((index, order) => ({
    index,
    width: 600,
    height: 800,
    kind: "native" as const,
    missing_layout: false,
    blocks:
      order === 0 || order === indices.length - 1 || order === 79
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
        source:
          p.index === 79 ? "Far away target keyword" : `Synthetic paragraph ${p.index} with Transformer keyword`,
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
    pdfMock.triggerErrorOnPage = null;
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

  it("在阅读器显示缺少版面解析结果的页码", async () => {
    const incomplete = makeDoc(3);
    incomplete.pages[1] = { ...incomplete.pages[1]!, missing_layout: true };
    await render(null, incomplete);
    expect(container.querySelector('.notice[role="status"]')?.textContent).toContain("第 2 页未返回版面解析结果");
  });

  async function render(
    spot: { id: string; n: number; term?: { source: string; target: string } } | null = null,
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

  it("滚动长文档时切换挂载窗口、更新页码，并在切显示模式缩放时保持阅读锚点", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-zoom");
    const scroller = container.querySelector<HTMLDivElement>(".pages")!;
    // 滚到约第 26 页（scrollTop = 20000）
    scroller.scrollTop = 20000;
    await act(async () => {
      scroller.dispatchEvent(new Event("scroll"));
      await vi.advanceTimersByTimeAsync(32);
    });
    const pageInput = container.querySelector<HTMLInputElement>('input[aria-label="跳到第几页"]')!;
    const pageBefore = Number(pageInput.value);
    expect(pageBefore).toBeGreaterThanOrEqual(24);
    expect(pageBefore).toBeLessThanOrEqual(28);

    // 从“对照”切换为“原文”（缩放翻倍）：应保持在同一页，不跳回第 13 页
    const origBtn = [...container.querySelectorAll<HTMLButtonElement>(".segmented button")].find(
      (b) => b.textContent === "原文",
    )!;
    await act(async () => {
      origBtn.click();
      await vi.advanceTimersByTimeAsync(32);
    });
    const pageAfter = Number(pageInput.value);
    expect(Math.abs(pageAfter - pageBefore)).toBeLessThanOrEqual(1);
    expect(scroller.scrollTop).toBeGreaterThan(35000);
    expect(container.querySelector(`.page-row[data-page="${pageBefore - 1}"]`)).not.toBeNull();
  });

  it("非 0 paddingTop 时当前页翻转扣除 paddingTop，且短距离平滑跳转首帧保留起点页", async () => {
    const longDoc = makeDoc(10);
    await render(null, longDoc, "padding-doc");
    const scroller = container.querySelector<HTMLDivElement>(".pages")!;
    scroller.style.paddingTop = "24px";

    // 每页高 800 * fitZoom；scrollTop = 24 相当于布局坐标 0（第 1 页）
    scroller.scrollTop = 24;
    await act(async () => {
      scroller.dispatchEvent(new Event("scroll"));
      await vi.advanceTimersByTimeAsync(32);
    });
    const pageInput = container.querySelector<HTMLInputElement>('input[aria-label="跳到第几页"]')!;
    expect(pageInput.value).toBe("1");

    // 跳到相邻第 2 页（短距离平滑跳转），首帧仍保留第 1 页不闪白
    await act(async () => {
      setInputValue(pageInput, "2");
      pageInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(scrollTo).toHaveBeenLastCalledWith({ top: expect.any(Number), behavior: "smooth" });
    expect(container.querySelector('.page-row[data-page="0"]')).not.toBeNull();
    expect(container.querySelector('.page-row[data-page="1"]')).not.toBeNull();
  });

  it("搜索防抖 150ms 后出结果，并可跳转到尚未挂载的第 80 页（远距离用 auto）", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-search");
    expect(container.querySelector('.page-row[data-page="79"]')).toBeNull();

    const searchBtn = container.querySelector<HTMLButtonElement>('button[title="搜索原文和译文"]');
    await act(async () => searchBtn?.click());
    const input = container.querySelector<HTMLInputElement>('input[aria-label="搜索原文或译文"]')!;

    await act(async () => setInputValue(input, "远端命中译文"));
    expect(container.querySelectorAll(".search-result").length).toBe(0);

    await act(async () => vi.advanceTimersByTimeAsync(160));
    const results = container.querySelectorAll<HTMLButtonElement>(".search-result");
    expect(results.length).toBe(1);

    await act(async () => results[0]?.click());
    expect(scrollTo).toHaveBeenCalledTimes(1);
    const arg = scrollTo.mock.calls[0]?.[0] as { top: number; behavior: string } | undefined;
    expect(arg?.top).toBeGreaterThan(10000);
    expect(arg?.behavior).toBe("auto");
    expect(container.querySelector('.page-row[data-page="79"]')).not.toBeNull();
  });

  it("搜索框忽略中文 IME 组合回车，正常 Shift+Enter 立即跳到末条命中并高亮", async () => {
    const longDoc = makeDoc(100);
    await render(null, longDoc, "long-doc-enter");
    const searchBtn = container.querySelector<HTMLButtonElement>('button[title="搜索原文和译文"]');
    await act(async () => searchBtn?.click());
    const input = container.querySelector<HTMLInputElement>('input[aria-label="搜索原文或译文"]')!;

    // IME 组合期间按 Enter 不触发搜索跳转
    await act(async () => {
      setInputValue(input, "Synthetic");
      const imeEvent = new KeyboardEvent("keydown", { key: "Enter", bubbles: true });
      Object.defineProperty(imeEvent, "isComposing", { value: true });
      input.dispatchEvent(imeEvent);
    });
    expect(scrollTo).not.toHaveBeenCalled();

    // 正常 Shift+Enter 立即跳转到最后一条命中
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true }));
    });
    expect(scrollTo).toHaveBeenCalledTimes(1);
    const countText = container.querySelector(".reader-search-count")?.textContent ?? "";
    expect(countText).toMatch(/^(\d+) \/ \1$/);
  });

  it("支持非连续 page.index 的恢复阅读位置、页码输入映射与 pagehide 落盘", async () => {
    const sparseDoc = makeDoc(2, [2, 5]);
    localStorage.setItem("pdftr.pos.v1", JSON.stringify({ sparse: 5 }));
    await render(null, sparseDoc, "sparse");

    // 自动恢复到真实 page.index = 5（第 2 页）
    await act(async () => vi.advanceTimersByTimeAsync(450));
    expect(scrollTo).toHaveBeenCalledTimes(1);
    const pageInput = container.querySelector<HTMLInputElement>('input[aria-label="跳到第几页"]')!;
    expect(pageInput.value).toBe("2");

    // 输入显示序号 1 应跳转到真实 page.index = 2
    await act(async () => {
      setInputValue(pageInput, "1");
      pageInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(scrollTo).toHaveBeenCalledTimes(2);

    // 触发 pagehide 应立即将最新页写入 localStorage 无需等 500ms
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    const stored = JSON.parse(localStorage.getItem("pdftr.pos.v1") ?? "{}") as Record<string, number>;
    expect(stored.sparse).toBe(2);
  });

  it("原生页渲染 PdfTextLayer（含术语高亮与异常降级），扫描页降级保留块级 .source-text-layer", async () => {
    pdfMock.originalDoc = { fake: true };
    const mixedDoc = makeDoc(2);
    mixedDoc.pages[1]!.kind = "scanned";
    await render({ id: "s1", n: 1, term: { source: "Transformer", target: "变换器" } }, mixedDoc, "mixed-doc");

    const nativePage = container.querySelector('.page-row[data-page="0"] .page[aria-label="原文第 1 页"]');
    const scannedPage = container.querySelector('.page-row[data-page="1"] .page[aria-label="原文第 2 页"]');
    expect(nativePage?.querySelector('[data-testid="pdf-text-layer-0"]')).not.toBeNull();
    expect(nativePage?.querySelector("mark.term-highlight")?.textContent).toBe("Transformer");
    expect(nativePage?.querySelector(".source-text-layer")).toBeNull();
    expect(scannedPage?.querySelector('[data-testid="pdf-text-layer-1"]')).toBeNull();
    expect(scannedPage?.querySelector(".source-text-layer")).not.toBeNull();

    // 当原生页 PdfTextLayer 报告错误时，自动降级为块级 .source-text-layer
    pdfMock.triggerErrorOnPage = 0;
    await render(null, makeDoc(1), "fallback-doc");
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.querySelector('[data-testid="pdf-text-layer-0"]')).toBeNull();
    expect(container.querySelector(".source-text-layer")).not.toBeNull();
  });

  it("原文页坐标命中触发单次选中与悬停联动，拖拽或选区非空时不误触（原生与扫描页一致）", async () => {
    pdfMock.originalDoc = { fake: true };
    const onSelect = vi.fn();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      return this.classList.contains("page") ? new DOMRect(100, 50, 600, 800) : new DOMRect();
    });
    const mixedDoc = makeDoc(2);
    mixedDoc.pages[1]!.kind = "scanned";
    await render(null, mixedDoc, "hit-doc", onSelect);

    const origPage = container.querySelector<HTMLElement>('.page-row[data-page="0"] .page[aria-label="原文第 1 页"]')!;
    await act(async () => {
      origPage.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(container.querySelector(".hit.hovered")).not.toBeNull();
    expect(container.querySelector(".tblock.hovered")).not.toBeNull();

    // 正常单击触发一次选中
    await act(async () => {
      origPage.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 150, clientY: 170 }));
      origPage.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("s1");

    // 拖拽位移超过阈值（即使 selection 已折叠）不触发选中
    await act(async () => {
      origPage.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 120, clientY: 170 }));
      origPage.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 180, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);

    // 选区非空时不触发选中
    const getSelectionSpy = vi
      .spyOn(window, "getSelection")
      .mockReturnValue({ toString: () => "Synthetic" } as unknown as Selection);
    await act(async () => {
      origPage.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    getSelectionSpy.mockRestore();

    // 扫描页单击同样只触发一次（无双触发）
    const scannedPage = container.querySelector<HTMLElement>(
      '.page-row[data-page="1"] .page[aria-label="原文第 2 页"]',
    )!;
    await act(async () => {
      scannedPage.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: 150, clientY: 170 }));
    });
    expect(onSelect).toHaveBeenCalledTimes(2);

    // 鼠标移出页面清除 hovered
    await act(async () => {
      origPage.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }));
    });
    expect(container.querySelector(".hit.hovered")).toBeNull();
  });
});
