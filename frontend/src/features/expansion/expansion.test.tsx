import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";

import { api, type Expansion, type Job } from "@/shared/api/client";
import { ConfirmProvider } from "@/shared/feedback/ConfirmDialog";

import { ExpandPagesDialog } from "./ExpandPagesDialog";
import { ExpansionBanner } from "./ExpansionBanner";

const job: Job = {
  id: "sample", status: "done", filename: "sample.pdf", stage: "done", progress: 1,
  message: "", error: "", page_count: 5, content_version: 1, fallback_used: false, model: "fixture", cost_cny: null, channel_mode: "free",
  created_at: "2026-01-01T00:00:00Z", stage_started_at: null, expires_at: null,
  options: {
    parser: "mineru-cloud", quality: "default", include_references: false,
    translate_figures: false, cloud_consent: true, pages: [0, 2],
  },
};
const op: Expansion = {
  id: "operation", job_id: job.id, status: "running", stage: "parse", progress: 0.5,
  requested_pages: [1, 3], merged_pages: [0, 1, 2, 3], base_version: 1, target_version: 2,
  message: "合成进度", error: "", cost_cny: null, fallback_used: false,
  created_at: job.created_at, updated_at: job.created_at,
};

let root: Root;
let host: HTMLDivElement;
let queries: QueryClient;
beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  queries.clear();
  vi.restoreAllMocks();
});

it.each([{ pages: null }, { pages: [1, 3] }])("补翻页码选择提交 %j", async ({ pages }) => {
  const start = vi.spyOn(api, "expandPages").mockResolvedValue(op);
  const estimate = vi.spyOn(api, "estimate");
  const onStarted = vi.fn();
  await act(async () => root.render(
    <QueryClientProvider client={queries}>
      <ExpandPagesDialog job={job} published={new Set([0, 2])} onStarted={onStarted} onClose={vi.fn()} />
    </QueryClientProvider>,
  ));
  const dialog = document.querySelector("dialog")!;
  const button = dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  if (pages) {
    await act(async () => dialog.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1]!.click());
    expect(button.disabled).toBe(true);
    const range = dialog.querySelector<HTMLInputElement>('input[placeholder]')!;
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    const enter = async (value: string) => act(async () => {
      set.call(range, value);
      range.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await enter("1, 3");
    expect(dialog.textContent).toContain("所选页面已全部翻译");
    expect(button.disabled).toBe(true);
    await enter("1-4");
    expect(dialog.textContent).toContain("实际补翻 2 页");
  } else {
    expect(dialog.textContent).toContain("补翻全部剩余 3 页");
  }
  await act(async () => {
    button.click();
    button.click();
  });
  expect(start).toHaveBeenCalledExactlyOnceWith(job.id, pages, "free");
  expect(estimate).not.toHaveBeenCalled();
  expect(onStarted).toHaveBeenCalledWith(op);
});

it("展示补翻进度并确认提交一次取消请求", async () => {
  const cancel = vi.spyOn(api, "cancelExpansion").mockResolvedValue(op);
  await act(async () => root.render(<ConfirmProvider><ExpansionBanner op={op} /></ConfirmProvider>));
  expect(host.textContent).toContain("正在补翻 2 页");
  expect(host.querySelector("progress")?.value).toBe(0.5);
  await act(async () => host.querySelector("button")!.click());
  const confirm = [...document.querySelectorAll("dialog button")].find(
    (button) => button.textContent === "确认取消补翻",
  ) as HTMLButtonElement;
  await act(async () => confirm.click());
  expect(cancel).toHaveBeenCalledExactlyOnceWith(job.id, op.id);
  expect(host.querySelector("button")?.disabled).toBe(true);
});
