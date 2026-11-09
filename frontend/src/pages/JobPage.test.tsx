import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { JobPage } from "@/pages/JobPage";
import { api, type DocumentData, type Job } from "@/shared/api/client";

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
  ReaderView: ({ heading, actions }: { heading: React.ReactNode; actions: React.ReactNode }) => (
    <div>
      {heading}
      <p>阅读器</p>
      {actions}
    </div>
  ),
}));
vi.mock("@/features/job-terms/TermsPanel", () => ({ TermsPanel: () => null }));
vi.mock("@/features/segment-edit/SegmentEditor", () => ({ SegmentEditor: () => null }));

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
});
