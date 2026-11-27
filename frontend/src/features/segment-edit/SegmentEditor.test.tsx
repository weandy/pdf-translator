import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type RetranslateSegment, type SegmentData } from "@/shared/api/client";

import { SegmentEditor } from "./SegmentEditor";

const segment = (overrides: Partial<RetranslateSegment> = {}): RetranslateSegment => ({
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
  applied: true,
  notice: "",
  ...overrides,
});

function Harness({ initial, channelMode }: { initial: SegmentData; channelMode?: "byok" | "free" }) {
  const [current, setCurrent] = useState(initial);
  return (
    <SegmentEditor
      key={`${current.id}:${current.target ?? ""}`}
      segment={current}
      channelMode={channelMode}
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

  it("正常一次翻译成功的段落隐藏内部尝试次数计数", async () => {
    await act(async () =>
      root.render(
        <Harness
          initial={segment({ status: "done", target: "正常译文", failure_reason: "", translation_attempts: 1 })}
        />,
      ),
    );
    expect(host.textContent).not.toContain("最近一次翻译实际尝试");
  });

  it.each(["done", "edited"] as const)("%s 旧译文重翻失败后保留译文并显示新失败原因", async (status) => {
    const initial = segment({ target: "上次有效译文", status, failure_reason: "", translation_attempts: 1 });
    vi.spyOn(api, "retranslate").mockResolvedValue({
      ...initial,
      applied: false,
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

  it("关闭面板中止请求并忽略迟到的成功响应", async () => {
    let finish!: (result: RetranslateSegment) => void;
    const request = vi.spyOn(api, "retranslate").mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    const change = vi.fn();
    await act(async () => root.render(<SegmentEditor segment={segment()} onChange={change} onClose={() => {}} />));
    const chip = [...host.querySelectorAll("button")].find((button) => button.textContent === "更通顺自然")!;
    await act(async () => chip.click());
    const field = host.querySelector<HTMLInputElement>('input[aria-label="修改要求"]')!;
    expect(field.maxLength).toBe(200);
    expect(field.value).toBe("更通顺自然");
    await act(async () => retryButton().click());
    const options = request.mock.calls[0]?.[3];
    expect(options?.instruction).toBe("更通顺自然");
    await act(async () => root.render(null));
    expect(options?.signal?.aborted).toBe(true);
    await act(async () => finish(segment({ target: "迟到的译文", status: "done" })));
    expect(change).not.toHaveBeenCalled();
  });

  it("重翻过程中解释重试上限并防止同一轮重复提交", async () => {
    let finish!: (value: RetranslateSegment) => void;
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

  it("免费任务重翻时透传 channelMode=free", async () => {
    const req = vi.spyOn(api, "retranslate").mockResolvedValue(segment({ target: "免费译文", status: "done" }));
    await act(async () => root.render(<Harness initial={segment()} channelMode="free" />));
    await act(async () => retryButton().click());
    expect(req).toHaveBeenCalledWith("synthetic", "default", expect.any(Object), expect.objectContaining({ channelMode: "free" }));
  });

  it("在编辑抽屉中按 Ctrl+S 直接触发保存并上报未保存状态", async () => {
    const edit = vi.spyOn(api, "editSegment").mockResolvedValue(segment({ target: "快捷键保存的译文", status: "edited" }));
    await act(async () => root.render(<Harness initial={segment({ target: "旧译文", status: "done" })} />));
    const textarea = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="译文"]')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      setter?.call(textarea, "快捷键保存的译文");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(host.textContent).toContain("未保存");
    const event = new KeyboardEvent("keydown", { key: "s", ctrlKey: true, bubbles: true, cancelable: true });
    await act(async () => {
      document.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
    expect(edit).toHaveBeenCalledWith("synthetic", "快捷键保存的译文", expect.any(AbortSignal));
  });

  it("含公式段落直接在译文框内所见即所得渲染公式，支持悬停看原始 LaTeX、点击修改、误删补回与双格式复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    const withMath = segment({
      source: "Vector of dimension <m1/>.",
      target: "维度为 <m1/> 的向量。",
      status: "done",
      failure_reason: "",
      translation_attempts: 1,
      formulas: { m1: { latex: "d_1", page: 0, bbox: { x0: 10, y0: 10, x1: 20, y1: 20 } } },
    });
    const edit = vi.spyOn(api, "editSegment").mockResolvedValue({
      ...withMath,
      status: "edited",
      formulas: { m1: { latex: "d_k", page: 0, bbox: { x0: 10, y0: 10, x1: 20, y1: 20 } } },
    });

    await act(async () => root.render(<Harness initial={withMath} />));

    // 不再显示冗余的常驻术语折叠表单与 <m1/> 占位符
    expect(host.querySelector(".segment-terms")).toBeNull();
    expect(host.querySelector(".wysiwyg-editor")?.textContent).not.toContain("<m1/>");

    // 复制译文应自动还原为标准 LaTeX $d_1$
    const copyTargetBtn = [...host.querySelectorAll("button")].find((b) => b.textContent === "复制译文")!;
    await act(async () => copyTargetBtn.click());
    expect(writeText).toHaveBeenCalledWith("维度为 $d_1$ 的向量。");

    // 鼠标悬停到译文内的公式上应仅浮现「复制」「修改」两个按钮（不冗余显示公式源码）
    const formulaNode = host.querySelector<HTMLElement>('.wysiwyg-formula[data-formula-key="m1"]')!;
    await act(async () => {
      formulaNode.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    });
    const tooltip = host.querySelector<HTMLElement>(".wysiwyg-formula-tooltip");
    expect(tooltip?.textContent).not.toContain("d_1");
    const tipCopyBtn = [...(tooltip?.querySelectorAll("button") ?? [])].find((b) => b.textContent === "复制")!;
    await act(async () => tipCopyBtn.click());
    expect(writeText).toHaveBeenLastCalledWith("$d_1$");

    // 点击译文内公式展开就地 LaTeX 修改条
    await act(async () => formulaNode.click());
    const latexInput = host.querySelector<HTMLTextAreaElement>('textarea[aria-label="公式 m1 的 LaTeX"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(latexInput, "\\frac{1");
      latexInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const saveBtn = [...host.querySelectorAll("button")].find((b) => b.textContent === "保存修改") as HTMLButtonElement;
    expect(host.textContent).toContain("LaTeX 语法有误");
    expect(saveBtn.disabled).toBe(true);

    // 改为合法 LaTeX 后实时刷新译文框公式，但原文区公式保持不变（只读隔离）
    await act(async () => {
      setter?.call(latexInput, "d_k");
      latexInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(formulaNode.dataset.latex).toBe("d_k");
    const sourceMath = host.querySelector<HTMLElement>(".editor-source-rendered [data-latex]");
    expect(sourceMath?.dataset.latex).toBe("d_1");
    expect(saveBtn.disabled).toBe(false);
    await act(async () => saveBtn.click());
    expect(edit).toHaveBeenCalledWith("synthetic", "维度为 <m1/> 的向量。", expect.any(AbortSignal), { m1: "d_k" });
  });
});
