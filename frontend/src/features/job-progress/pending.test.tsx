import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterAll, beforeAll, expect, it, vi } from "vitest";

import type { Job } from "@/shared/api/client";

import { isMineruCloudQueued, useMineruQueueElapsed } from "./pending";

beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

const message = "MinerU 已接收任务，正在等待解析资源（队列中）";
const job = { id: "synthetic", status: "running", stage: "parse", options: { parser: "mineru-cloud" }, message } as Job;

it.each([
  "MinerU 正在接收上传文件（waiting-file）", "正在上传到 MinerU", "免费通道排队（MinerU）",
  "排队等待解析", "MinerU 正在解析，第 2 页", "MinerU 正在整理解析结果",
  "MinerU 超时，正在重试（队列中）", "正在等待解析资源",
])("排除非云队列状态 %s", (status) => expect(isMineruCloudQueued(status)).toBe(false));

it.each([-300000, 300000])("服务端计时在刷新与校准时不受本机时间偏移 %s 影响", async (offset) => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance", "Date"] });
  const host = document.createElement("div");
  const root = createRoot(host);
  function Probe({ current }: { current: Job }) {
    return <output>{Math.floor(useMineruQueueElapsed(current))}</output>;
  }
  const render = (current: Job) => act(() => root.render(<Probe current={current} />));
  try {
    vi.setSystemTime(Date.now() + offset);
    await render({ ...job, parse_queue_elapsed_s: 24.9 });
    expect(host.textContent).toBe("0");
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(host.textContent).toBe("25000");
    await render({ ...job, parse_queue_elapsed_s: 36.2 });
    expect(host.textContent).toBe("36200");
    await render({ ...job, parse_queue_elapsed_s: null });
    expect(host.textContent).toBe("0");
  } finally {
    await act(() => root.unmount());
    vi.useRealTimers();
  }
});

it("从连续观察计时，25 秒展示并在退出云端队列后立即归零", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "performance", "Date"] });
  const host = document.createElement("div");
  const root = createRoot(host);
  function Probe({ current }: { current: Job }) {
    return <output>{Math.floor(useMineruQueueElapsed(current))}</output>;
  }
  const render = (current: Job) => act(() => root.render(<Probe current={current} />));
  const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
  try {
    await render({ ...job, message: "MinerU 正在接收上传文件" });
    await advance(60000);
    expect(host.textContent).toBe("0");
    await render(job);
    await advance(24900);
    vi.setSystemTime(Date.now() + 300000);
    expect(host.textContent).toBe("0");
    await advance(100);
    expect(host.textContent).toBe("25000");
    await render({ ...job, message: "MinerU 正在解析" });
    expect(host.textContent).toBe("0");
    await render(job);
    await advance(24999);
    expect(host.textContent).toBe("0");
    await advance(1);
    expect(host.textContent).toBe("25000");
  } finally {
    await act(() => root.unmount());
    vi.useRealTimers();
  }
});
