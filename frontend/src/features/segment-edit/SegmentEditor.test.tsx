import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type SegmentData } from "@/shared/api/client";

import { SegmentEditor } from "./SegmentEditor";

const segment = (overrides: Partial<SegmentData> = {}): SegmentData => ({
  id: "synthetic",
  block_ids: [],
  parts: [],
  source: "Synthetic source text.",
  target: null,
  status: "failed",
  quality: "default",
  formulas: {},
  translation_attempts: 10,
  failure_reason: "模型返回的公式不完整",
  ...overrides,
});

function Harness({ initial }: { initial: SegmentData }) {
  const [current, setCurrent] = useState(initial);
  return (
    <SegmentEditor
      key={`${current.id}:${current.target ?? ""}`}
      segment={current}
      onChange={setCurrent}
      onClose={() => {}}
    />
  );
}

describe("段落翻译失败反馈", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    host = document.createElement("div");
    root = createRoot(host);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
  });

  const retryButton = () => [...host.querySelectorAll("button")].find((b) => b.textContent === "重翻")!;

  it("显示未完成原因与最近实际尝试次数", async () => {
    await act(async () => root.render(<Harness initial={segment()} />));
    expect(host.textContent).toContain("未完成翻译");
    expect(host.textContent).toContain("最近一次翻译实际尝试 10 次");
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("模型返回的公式不完整");
  });

  it.each(["done", "edited"] as const)("%s 旧译文重翻失败后保留译文并显示新失败原因", async (status) => {
    const initial = segment({ target: "上次有效译文", status, failure_reason: "", translation_attempts: 1 });
    vi.spyOn(api, "retranslate").mockResolvedValue({
      ...initial,
      failure_reason: "本次模型持续返回空译文",
      translation_attempts: 10,
    });
    await act(async () => root.render(<Harness initial={initial} />));
    await act(async () => retryButton().click());
    expect((host.querySelector('[aria-label="译文"]') as HTMLTextAreaElement).value).toBe("上次有效译文");
    expect(host.textContent).toContain("本次重翻未成功，已保留上次译文");
    expect(host.textContent).toContain("本次模型持续返回空译文");
    expect(host.textContent).toContain("最近一次翻译实际尝试 10 次");
  });

  it("重翻过程中解释重试上限并防止同一轮重复提交", async () => {
    let finish!: (value: SegmentData) => void;
    const request = vi.spyOn(api, "retranslate").mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    await act(async () => root.render(<Harness initial={segment()} />));
    const button = retryButton();
    await act(async () => {
      button.click();
      button.click();
    });
    expect(request).toHaveBeenCalledTimes(1);
    expect(button.disabled).toBe(true);
    expect(host.querySelector('[role="status"]')?.textContent).toContain("最多尝试 10 次");
    expect(host.textContent).toContain("最多等待 45 秒");
    expect(host.textContent).not.toContain("模型返回的公式不完整");
    await act(async () => finish(segment({ target: "合成译文", status: "done", failure_reason: "" })));
    expect(host.textContent).not.toContain("正在翻译，失败时会自动重试");
  });

  it("本地请求错误与持久失败原因相同时只显示一次", async () => {
    vi.spyOn(api, "retranslate").mockRejectedValue(new Error("模型返回的公式不完整"));
    await act(async () => root.render(<Harness initial={segment()} />));
    await act(async () => retryButton().click());
    expect(host.textContent?.split("模型返回的公式不完整")).toHaveLength(2);
    expect(host.querySelectorAll('[role="alert"]')).toHaveLength(1);
  });
});
