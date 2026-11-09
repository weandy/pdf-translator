import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { Revision } from "@/shared/api/client";

import { RevisionList } from "./RevisionList";

const revision = (overrides: Partial<Revision> = {}): Revision => ({
  id: 1,
  segment_id: "synthetic",
  action: "retranslate",
  before: "采用旧方法求解。",
  after: "采用新方法求解。",
  before_status: "done",
  after_status: "done",
  reverts: null,
  reverted_by: null,
  created_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

describe("修改记录的变化展示", () => {
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

  async function render(revisions = [revision()], onRevert = vi.fn().mockResolvedValue(undefined), onOpen = vi.fn()) {
    await act(async () =>
      root.render(
        <RevisionList
          revisions={revisions}
          labels={{ synthetic: "第 1 页：合成段落" }}
          onRevert={onRevert}
          onOpen={onOpen}
        />,
      ),
    );
    return { onRevert, onOpen };
  }

  it("折叠预览仅强调变化字符，完整前后对比保持相同内容普通显示", async () => {
    await render();
    const preview = host.querySelector('[aria-label="译文变化预览"]')!;
    expect(preview.querySelector("del")?.textContent).toBe("旧");
    expect(preview.querySelector("ins")?.textContent).toBe("新");
    expect(host.textContent).toContain("用粗体、浅底和点状下划线标记");
    expect(host.querySelector("details")?.open).toBe(false);
    await act(async () => host.querySelector("summary")!.click());
    expect(host.querySelector("details")?.open).toBe(true);
    expect(host.querySelector('[aria-label="修改前译文"]')?.textContent).toBe("采用旧方法求解。");
    expect(host.querySelector('[aria-label="修改后译文"]')?.textContent).toBe("采用新方法求解。");
    expect(host.querySelector('[aria-label="修改前译文"] ins')).toBeNull();
    expect(host.querySelector('[aria-label="修改后译文"] del')).toBeNull();
  });

  it("段尾变化直接出现在预览，完整对比不再截断到80字", async () => {
    const prefix = "合成的相同上下文".repeat(30);
    await render([revision({ before: `${prefix}旧段尾`, after: `${prefix}新段尾` })]);
    expect(host.querySelector('[aria-label="译文变化预览"]')?.textContent).toContain("旧新段尾");
    expect(host.querySelector('[aria-label="修改后译文"]')?.textContent).toBe(`${prefix}新段尾`);
  });

  it("失败重翻而内容未变时明确说明，不把整段都标为新增", async () => {
    await render([revision({ before: "上次有效译文", after: "上次有效译文" })]);
    expect(host.textContent).toContain("译文内容未变化");
    expect(host.querySelector('[aria-label="译文变化预览"]')).toBeNull();
    expect(host.querySelector('[aria-label="修改后译文"] ins')).toBeNull();
  });

  it("完整新增、删除及公式原样展示，文本中的HTML不作为标签执行", async () => {
    await render([revision({ before: null, after: "<m12/><img src=x onerror=alert(1)>" })]);
    expect(host.querySelector('[aria-label="修改前译文"]')?.textContent).toBe("（空）");
    expect(host.querySelector('[aria-label="修改后译文"] ins')?.textContent).toBe("<m12/><img src=x onerror=alert(1)>");
    expect(host.querySelector("img")).toBeNull();
    await render([revision({ before: "删除全文", after: null })]);
    expect(host.querySelector('[aria-label="修改前译文"] del')?.textContent).toBe("删除全文");
    expect(host.querySelector('[aria-label="修改后译文"]')?.textContent).toBe("（空）");
  });

  it("保留定位、撤回和已撤回状态的操作约束", async () => {
    const current = revision();
    const { onRevert, onOpen } = await render([current]);
    await act(async () => host.querySelector<HTMLButtonElement>(".rev-target")!.click());
    expect(onOpen).toHaveBeenCalledWith("synthetic");
    await act(async () => [...host.querySelectorAll("button")].find((b) => b.textContent === "撤回这一步")!.click());
    expect(onRevert).toHaveBeenCalledWith(current);
    await render([revision({ reverted_by: 2 })]);
    expect(host.querySelector("li.voided")).not.toBeNull();
    expect(host.textContent).toContain("已撤回");
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "撤回这一步")).toBe(false);
  });

  it("长段落的完整对比说明按整块标记，空记录保持原提示", async () => {
    await render([revision({ before: "甲".repeat(1000), after: "乙".repeat(1000) })]);
    expect(host.textContent).toContain("段落较长，按整块标记变化");
    await render([]);
    expect(host.textContent).toBe("还没有手动修改。");
  });
});
