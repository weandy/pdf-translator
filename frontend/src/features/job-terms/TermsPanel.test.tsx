import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type DocumentData, type SegmentData } from "@/shared/api/client";

import { TermsPanel } from "./TermsPanel";

const segment = (id: string, overrides: Partial<SegmentData> = {}): SegmentData => ({
  id,
  block_ids: [],
  parts: [],
  source: "Synthetic term in source.",
  target: "合成术语译文",
  status: "done",
  quality: "default",
  formulas: {},
  translation_attempts: 1,
  failure_reason: "",
  ...overrides,
});

describe("术语批量重翻结果", () => {
  let host: HTMLDivElement;
  let root: Root;
  let client: QueryClient;
  const changed = vi.fn();
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    vi.useFakeTimers();
    host = document.createElement("div");
    root = createRoot(host);
    client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    changed.mockClear();
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  async function render(count = 3) {
    const segments = Array.from({ length: count }, (_, i) => segment(`s${i + 1}`));
    const uses = [
      { source: "Synthetic", target: "合成术语", origin: "paper" as const, segment_ids: segments.map((s) => s.id) },
    ];
    client.setQueryData(["terms", "synthetic"], uses);
    vi.spyOn(api, "jobTerms").mockResolvedValue(uses);
    const doc: DocumentData = {
      job: {} as DocumentData["job"],
      title: "合成文档",
      pages: [],
      segments,
      links: [],
      outline: [],
    };
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <TermsPanel
            jobId="synthetic"
            doc={doc}
            onChange={changed}
            onSpot={() => {}}
            onMark={() => {}}
            onClose={() => {}}
          />
        </QueryClientProvider>,
      ),
    );
    await act(async () => (host.querySelector(".term-item") as HTMLButtonElement).click());
    return segments;
  }

  const retryButton = () => [...host.querySelectorAll("button")].find((b) => b.textContent?.startsWith("重翻全部"))!;

  it("区分成功、无译文失败和保留旧译文的失败，并全部刷新页面数据", async () => {
    const segments = await render();
    vi.spyOn(api, "retranslateMany").mockResolvedValue([
      segments[0]!,
      { ...segments[1]!, target: null, status: "failed", failure_reason: "译文不完整", translation_attempts: 10 },
      { ...segments[2]!, failure_reason: "本次重翻超时", translation_attempts: 4 },
    ]);
    await act(async () => retryButton().click());
    expect(changed).toHaveBeenCalledTimes(3);
    expect(host.textContent).toContain("重翻完成：1 段成功，2 段未成功");
    expect(host.textContent).toContain("请定位段落查看原因");
    expect(host.textContent).not.toContain("可以在修改记录里逐段撤回");
  });

  it("全部成功才显示成功结果，缺失返回的段落也不能当作成功", async () => {
    const segments = await render();
    const request = vi.spyOn(api, "retranslateMany").mockResolvedValue(segments);
    await act(async () => retryButton().click());
    expect(host.textContent).toContain("重翻完成：3 段成功，0 段未成功");
    request.mockResolvedValue(segments.slice(0, 2));
    await act(async () => retryButton().click());
    expect(host.textContent).toContain("重翻完成：2 段成功，1 段未成功");
  });

  it("后续批次异常时保留已处理结果和未处理数量", async () => {
    const segments = await render(21);
    const request = vi
      .spyOn(api, "retranslateMany")
      .mockResolvedValueOnce(segments.slice(0, 20))
      .mockRejectedValueOnce(new Error("合成网络错误"));
    await act(async () => retryButton().click());
    expect(request).toHaveBeenCalledTimes(2);
    expect(changed).toHaveBeenCalledTimes(20);
    expect(host.textContent).toContain("已处理 20 段：20 段成功，0 段未成功；另有 1 段未处理");
    expect(host.textContent).toContain("合成网络错误");
    expect(host.textContent).not.toContain("重翻完成");
  });

  it("进行中说明自动重试上限并阻止重复请求", async () => {
    const segments = await render();
    let finish!: (value: SegmentData[]) => void;
    const request = vi
      .spyOn(api, "retranslateMany")
      .mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    const button = retryButton();
    await act(async () => {
      button.click();
      button.click();
    });
    expect(request).toHaveBeenCalledTimes(1);
    expect(button.disabled).toBe(true);
    expect(host.textContent).toContain("单段最多尝试 10 次");
    await act(async () => finish(segments));
  });
});
