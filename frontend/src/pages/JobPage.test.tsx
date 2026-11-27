import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { JobPage } from "@/pages/JobPage";
import type { ReaderView } from "@/features/reader/ReaderView";
import { api, type DocumentData, type Expansion, type Job, type SegmentData } from "@/shared/api/client";

import { ConfirmProvider } from "@/shared/feedback/ConfirmDialog";
import { ToastProvider } from "@/shared/feedback/Toast";

const job: Job = {
  id: "synthetic",
  status: "done",
  filename: "synthetic.pdf",
  stage: "render",
  progress: 1,
  message: "",
  error: "",
  content_version: 1,
  page_count: 1,
  model: "synthetic",
  cost_cny: null,
  channel_mode: "byok",
  fallback_used: false,
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

let currentJob = job;
const refresh = vi.fn();

vi.mock("@/entities/job/model", () => ({
  useJob: () => ({
    job: currentJob,
    error: null,
    refresh,
  }),
  STAGE_LABELS: {},
}));
vi.mock("@/entities/settings/model", () => ({
  useSettings: () => ({ translationSide: "left" }),
  savePrefs: vi.fn(),
}));
vi.mock("@/features/reader/ReaderView", () => ({
  ReaderView: ({
    heading,
    actions,
    spot,
    backgroundUrl,
    selectedSegment,
    editingOpen,
    onSelectSegment,
    onEditSegment,
  }: ComponentProps<typeof ReaderView>) => (
    <div
      data-spot={spot?.id}
      data-spot-count={spot?.n}
      data-pdf={backgroundUrl}
      data-editable={Boolean(onSelectSegment)}
      data-selected={selectedSegment ?? ""}
      data-editing={String(Boolean(editingOpen))}
    >
      {heading}
      <p>阅读器</p>
      {onSelectSegment && (
        <button type="button" data-testid="mock-click-first" onClick={() => onSelectSegment("first")}>
          单击首段
        </button>
      )}
      {onEditSegment && (
        <button type="button" data-testid="mock-edit-first" onClick={() => onEditSegment("first")}>
          校对首段
        </button>
      )}
      {onSelectSegment && (
        <button type="button" data-testid="mock-click-middle" onClick={() => onSelectSegment("middle")}>
          单击中段
        </button>
      )}
      {actions}
    </div>
  ),
}));
vi.mock("@/features/job-terms/TermsPanel", () => ({
  TermsPanel: ({ hidden, onEditSegment, onClose }: { hidden?: boolean; onEditSegment?: (id: string) => void; onClose: () => void }) => (
    <aside data-testid="mock-terms" hidden={hidden}>
      <span>术语面板</span>
      {onEditSegment && (
        <button type="button" onClick={() => onEditSegment("first")}>
          术语编辑首段
        </button>
      )}
      <button type="button" onClick={onClose}>
        关闭术语
      </button>
    </aside>
  ),
}));

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
    currentJob = job;
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
          <ConfirmProvider>
            <ToastProvider>
              <MemoryRouter initialEntries={["/jobs/synthetic"]}>
                <Routes>
                  <Route path="/jobs/:id" element={<JobPage />} />
                </Routes>
              </MemoryRouter>
            </ToastProvider>
          </ConfirmProvider>
        </QueryClientProvider>,
      ),
    );
  }

  it("保留阅读器挂载并在发布后切换阅读数据、背景和导出版本", async () => {
    const prior = { ...job, page_count: 3, options: { ...job.options, pages: [0] }, content_version: 1 };
    const op: Expansion = {
      id: "operation", job_id: job.id, status: "running", stage: "translate", progress: 0.5,
      requested_pages: [1, 2], merged_pages: [0, 1, 2], base_version: 1, target_version: 2,
      message: "补翻中", error: "", cost_cny: null, fallback_used: false,
      created_at: job.created_at, updated_at: job.created_at,
    };
    currentJob = prior;
    client.setQueryData(["document", job.id, 1], { ...failedDoc(), job: prior });
    const finalJob = { ...prior, content_version: 2, published_pages: [0, 1, 2], options: { ...job.options, pages: null } };
    let resolveDocument: (doc: DocumentData) => void = () => undefined;
    vi.spyOn(api, "expansionStatus").mockResolvedValue({ ...op, status: "done", stage: "done" });
    const documentRequest = vi.spyOn(api, "document").mockResolvedValueOnce({ ...failedDoc(), job: prior })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveDocument = resolve; }));
    await render();
    const reader = host.querySelector("[data-pdf]");
    expect(reader?.getAttribute("data-pdf")).toContain("?v=1");
    expect(host.textContent).toContain("仅翻译了 1/3 页");
    await click("下载 PDF");
    expect(document.querySelector(".download-menu[role=menu]")).not.toBeNull();
    currentJob = { ...prior, active_expansion: op };
    await render();
    expect(host.querySelector("[data-pdf]")).toBe(reader);
    expect(reader?.getAttribute("data-editable")).toBe("false");
    expect(host.textContent).toContain("正在补翻 2 页");
    expect(document.querySelector(".download-menu[role=menu]")).not.toBeNull();
    currentJob = finalJob;
    await render();
    expect(host.querySelector("[data-pdf]")).toBe(reader);
    expect(reader?.getAttribute("data-pdf")).toContain("?v=1");
    expect(document.querySelector(".download-menu[role=menu]")).toBeNull();
    await act(async () => {
      resolveDocument({ ...failedDoc(), job: finalJob });
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(documentRequest).toHaveBeenCalledTimes(2);
    expect(host.querySelector("[data-pdf]")).toBe(reader);
    expect(reader?.getAttribute("data-pdf")).toContain("?v=2");
    expect(reader?.getAttribute("data-editable")).toBe("true");
    expect(document.querySelector(".toast-stack")?.textContent).toContain("补翻完成，已更新为 3/3 页译文");
    expect(client.getQueryData(["revisions", job.id, 2])).toEqual([]);
  });

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
    client.setQueryData(["document", "synthetic", 1], doc);
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
    client.setQueryData(["document", "synthetic", 1], doc);
    vi.spyOn(api, "document").mockResolvedValue(doc);
    await render();
    expect(host.textContent).toContain("阅读器");
    expect(host.textContent).not.toContain("已翻译 0/0 段");
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
    vi.spyOn(api, "retranslate").mockResolvedValue({ ...doc.segments[1]!, target: "合成译文", status: "done", applied: true, notice: "" });
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
    client.setQueryData(["document", "synthetic", 1], doc);
    vi.spyOn(api, "document").mockResolvedValue(doc);
    const retranslate = vi.spyOn(api, "retranslate").mockRejectedValueOnce(new Error("合成服务失败"));
    await render();
    await click("1 段未完成翻译 · 点击定位");
    expect(host.textContent).not.toContain("下一处");
    await click("重翻");
    expect(host.textContent).toContain("合成服务失败");
    expect(host.textContent).toContain("待处理 1 / 1");
    retranslate.mockResolvedValue({ ...doc.segments[0]!, target: "合成译文", status: "done", applied: true, notice: "" });
    await click("重翻");
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(host.textContent).not.toContain("未完成翻译");
    expect(host.textContent).toContain("待处理段落已全部处理");
    expect(host.textContent).not.toContain("已翻译 1/1 段");
  });

  it("纯阅读态单击段落仅聚焦不弹编辑抽屉，进入精校后单击切段，未保存修改离开前弹确认拦截", async () => {
    await renderFailed();
    // 纯阅读态单击首段：高亮聚焦但不打开右侧编辑抽屉
    await click("单击首段");
    expect(host.querySelector("[data-selected]")?.getAttribute("data-selected")).toBe("first");
    expect(host.querySelector("[data-editing]")?.getAttribute("data-editing")).toBe("false");
    expect(host.querySelector('[aria-label="段落编辑"]')).toBeNull();

    // 显式触发校对（双击或点浮动胶囊）打开编辑抽屉
    await click("校对首段");
    expect(host.querySelector("[data-editing]")?.getAttribute("data-editing")).toBe("true");
    expect(host.querySelector('[aria-label="原文"]')?.textContent).toBe("Synthetic first");

    // 精校态下无未保存修改时，单击中段直接切换
    await click("单击中段");
    expect(host.querySelector('[aria-label="原文"]')?.textContent).toBe("Synthetic middle");

    // 输入未保存草稿后，尝试关闭抽屉会弹出确认框拦截；点取消留在原段，点放弃修改才关闭
    const textarea = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="译文"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      setter?.call(textarea, "未保存的草稿");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(host.textContent).toContain("未保存");

    await click("关闭");
    expect(document.querySelector(".confirm-dialog")).not.toBeNull();
    expect(document.body.textContent).toContain("放弃未保存的修改");
    const cancelBtn = [...document.querySelectorAll(".confirm-actions button")].find((b) => b.textContent === "继续编辑") as HTMLButtonElement;
    await act(async () => cancelBtn.click());
    expect(host.querySelector('[aria-label="段落编辑"]')).not.toBeNull();

    await click("关闭");
    const discardBtn = [...document.querySelectorAll(".confirm-actions button")].find((b) => b.textContent === "放弃修改") as HTMLButtonElement;
    await act(async () => discardBtn.click());
    expect(host.querySelector('[aria-label="段落编辑"]')).toBeNull();
  });

  it("从本文术语进入段落编辑时保留术语面板挂载，返回后无缝切回术语上下文", async () => {
    await renderFailed();
    await click("术语");
    const termsAside = host.querySelector('[data-testid="mock-terms"]') as HTMLElement;
    expect(termsAside).not.toBeNull();
    expect(termsAside.hidden).toBe(false);

    await click("术语编辑首段");
    expect(host.querySelector('[aria-label="段落编辑"]')).not.toBeNull();
    expect(host.textContent).toContain("返回「本文术语」");
    // 术语面板 DOM 保持挂载但隐藏，筛选和展开状态不丢失
    expect(host.querySelector('[data-testid="mock-terms"]')).toBe(termsAside);
    expect(termsAside.hidden).toBe(true);

    await click("返回「本文术语」");
    expect(host.querySelector('[aria-label="段落编辑"]')).toBeNull();
    expect(termsAside.hidden).toBe(false);
  });
});
