import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { JobPage } from "@/pages/JobPage";
import type { ReaderView } from "@/features/reader/ReaderView";
import { api, type DocumentData, type Job, type SegmentData } from "@/shared/api/client";

const job: Job = {
  id: "synthetic",
  status: "done",
  filename: "synthetic.pdf",
  stage: "render",
  progress: 1,
  message: "",
  error: "",
  page_count: 1,
  model: "synthetic",
  cost_cny: null,
  created_at: "2026-01-01T00:00:00Z",
  stage_started_at: null,
  expires_at: null,
  options: {
    parser: "mineru-cloud",
    quality: "default",
    include_references: false,
    translate_figures: false,
    cloud_consent: true,
  },
};

vi.mock("@/entities/job/model", () => ({
  useJob: () => ({
    job,
    error: null,
    refresh: vi.fn(),
  }),
  STAGE_LABELS: {},
}));
vi.mock("@/entities/settings/model", () => ({ useSettings: () => ({ translationSide: "left" }) }));
vi.mock("@/features/reader/ReaderView", () => ({
  ReaderView: ({ heading, actions, spot }: ComponentProps<typeof ReaderView>) => (
    <div data-spot={spot?.id} data-spot-count={spot?.n}>
      {heading}
      <p>阅读器</p>
      {actions}
    </div>
  ),
}));
vi.mock("@/features/job-terms/TermsPanel", () => ({ TermsPanel: () => null }));

function failedDoc(): DocumentData {
  return {
    job,
    title: "合成文档",
    pages: [],
    links: [],
    outline: [],
    segments: ["first", "middle", "last"].map((id): SegmentData => ({
      id,
      block_ids: [],
      parts: [],
      source: `Synthetic ${id}`,
      target: null,
      status: "failed",
      quality: "default",
      formulas: {},
      translation_attempts: 0,
      failure_reason: "",
    })),
  };
}

describe("任务完成后的阅读数据等待", () => {
  let host: HTMLDivElement;
  let root: Root;
  let client: QueryClient;
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    vi.useFakeTimers();
    host = document.createElement("div");
    root = createRoot(host);
    // 即使全局允许重试，此页面也必须关闭自动重试。
    client = new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 10 } } });
    vi.spyOn(api, "revisions").mockResolvedValue([]);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  async function render() {
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={["/jobs/synthetic"]}>
            <Routes>
              <Route path="/jobs/:id" element={<JobPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      ),
    );
  }

  function checkDownloads() {
    expect(host.querySelector('a[href="/api/jobs/synthetic/export?mode=mono"]')).not.toBeNull();
    expect(host.querySelector('a[href="/api/jobs/synthetic/export?mode=dual&side=left"]')).not.toBeNull();
    expect(host.querySelector('a[href="/api/jobs/synthetic/files/original.pdf"]')).not.toBeNull();
  }

  async function click(label: string) {
    const button = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
    expect(button, label).toBeDefined();
    await act(async () => button!.click());
  }

  async function renderFailed() {
    const doc = failedDoc();
    client.setQueryData(["document", "synthetic"], doc);
    vi.spyOn(api, "document").mockResolvedValue(doc);
    await render();
    return doc;
  }

  it("加载期间明确已完成，保留下载入口；八秒后说明等待，卸载时取消请求", async () => {
    const document = vi.spyOn(api, "document").mockImplementation(() => new Promise(() => {}));
    await render();
    expect(host.textContent).toContain("翻译已完成");
    expect(host.textContent).toContain("正在打开译文");
    checkDownloads();
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(host.textContent).toContain("首次整理文档链接可能稍慢");
    const signal = document.mock.calls[0]![1]!;
    await act(async () => root.render(null));
    expect(signal.aborted).toBe(true);
  });

  it("失败后不自动重试或展示服务端原始错误，用户可手动重新打开", async () => {
    const document = vi.spyOn(api, "document").mockRejectedValue(new Error("synthetic-sensitive-detail"));
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(document).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("翻译已完成");
    expect(host.textContent).toContain("暂时无法打开阅读器");
    expect(host.textContent).not.toContain("synthetic-sensitive-detail");
    checkDownloads();
    await act(async () => host.querySelector("button")!.click());
    expect(document).toHaveBeenCalledTimes(2);
  });

  it("阅读数据就绪后直接展示阅读器，仅保留工具栏中的下载入口", async () => {
    const doc: DocumentData = { job, title: "合成文档", pages: [], segments: [], links: [], outline: [] };
    client.setQueryData(["document", "synthetic"], doc);
    vi.spyOn(api, "document").mockResolvedValue(doc);
    await render();
    expect(host.textContent).toContain("阅读器");
    expect(host.textContent).toContain("已翻译 0/0 段");
    expect(host.textContent).toContain("下载 PDF");
    expect(host.textContent).not.toContain("开始阅读");
    expect(host.querySelector(".completion-bar")).toBeNull();
  });

  it("点击校验提示打开问题段落，支持循环前后定位与重复定位同一段", async () => {
    await renderFailed();
    await click("3 段未完成翻译 · 点击定位");
    expect(host.querySelector('[aria-label="原文"]')?.textContent).toBe("Synthetic first");
    expect(host.querySelector("[data-spot]")?.getAttribute("data-spot")).toBe("first");
    expect(host.textContent).toContain("待处理 1 / 3");
    await click("上一处");
    expect(host.textContent).toContain("待处理 3 / 3");
    expect(host.querySelector("[data-spot]")?.getAttribute("data-spot")).toBe("last");
    await click("下一处");
    expect(host.textContent).toContain("待处理 1 / 3");
    const n = Number(host.querySelector("[data-spot]")?.getAttribute("data-spot-count"));
    await click("3 段未完成翻译 · 点击定位");
    expect(Number(host.querySelector("[data-spot]")?.getAttribute("data-spot-count"))).toBe(n + 1);
  });

  it("中间段重翻成功后更新数量，保留当前译文，继续检查时不跳过后一段", async () => {
    const doc = await renderFailed();
    vi.spyOn(api, "retranslate").mockResolvedValue({ ...doc.segments[1]!, target: "合成译文", status: "done" });
    await click("3 段未完成翻译 · 点击定位");
    await click("下一处");
    await click("重翻");
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(host.textContent).toContain("2 段未完成翻译");
    expect(host.textContent).toContain("本段已处理，还剩 2 段");
    expect(host.querySelector('[aria-label="译文"]')?.textContent).toBe("合成译文");
    expect(host.querySelector("[data-spot]")?.getAttribute("data-spot")).toBe("middle");
    await click("查看下一处");
    expect(host.querySelector("[data-spot]")?.getAttribute("data-spot")).toBe("last");
  });

  it("最后一段修好后移除问题入口；重翻失败保留原段及待处理数量", async () => {
    const doc = failedDoc();
    doc.segments = doc.segments.slice(0, 1);
    client.setQueryData(["document", "synthetic"], doc);
    vi.spyOn(api, "document").mockResolvedValue(doc);
    const retranslate = vi.spyOn(api, "retranslate").mockRejectedValueOnce(new Error("合成服务失败"));
    await render();
    await click("1 段未完成翻译 · 点击定位");
    expect(host.textContent).not.toContain("下一处");
    await click("重翻");
    expect(host.textContent).toContain("合成服务失败");
    expect(host.textContent).toContain("待处理 1 / 1");
    retranslate.mockResolvedValue({ ...doc.segments[0]!, target: "合成译文", status: "done" });
    await click("重翻");
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(host.textContent).not.toContain("未完成翻译");
    expect(host.textContent).toContain("待处理段落已全部处理");
    expect(host.textContent).toContain("已翻译 1/1 段");
  });
});
