import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ReaderView } from "@/features/reader/ReaderView";
import type { DocumentData } from "@/shared/api/client";

vi.mock("@/shared/pdf/pdf", () => ({ usePdf: () => ({ doc: null, error: null }), PdfCanvas: () => null }));

const doc: DocumentData = {
  job: {} as DocumentData["job"],
  title: "合成文档",
  links: [],
  outline: [],
  pages: Array.from({ length: 3 }, (_, index) => ({
    index,
    width: 600,
    height: 800,
    kind: "native",
    blocks:
      index === 0
        ? [
            {
              id: "b1",
              page: 0,
              type: "text",
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
  })),
  segments: [
    {
      id: "s1",
      block_ids: ["b1"],
      parts: [{ block_id: "b1", chars: 10 }],
      source: "Synthetic paragraph",
      target: null,
      status: "failed",
      quality: "default",
      formulas: {},
    },
  ],
};

describe("阅读位置恢复与手动段落定位", () => {
  let root: Root;
  const scrollTo = vi.fn();
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.setItem("pdftr.pos.v1", JSON.stringify({ synthetic: 2 }));
    scrollTo.mockClear();
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
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => setTimeout(() => callback(0), 16));
    vi.stubGlobal("cancelAnimationFrame", (handle: number) => clearTimeout(handle));
    Object.defineProperty(Element.prototype, "scrollTo", { configurable: true, value: scrollTo });
    root = createRoot(document.createElement("div"));
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    Reflect.deleteProperty(Element.prototype, "scrollTo");
    localStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  async function render(spot: { id: string; n: number } | null = null) {
    await act(async () =>
      root.render(
        <ReaderView doc={doc} jobId="synthetic" selectedSegment={null} onSelectSegment={() => {}} spot={spot} />,
      ),
    );
  }

  it("未操作时恢复上次阅读位置", async () => {
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(450));
    expect(scrollTo).toHaveBeenCalledExactlyOnceWith({ top: 0, behavior: "auto" });
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
});
