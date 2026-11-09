import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/shared/api/client";

describe("阅读数据请求的等待与取消", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function pendingFetch() {
    const fetch = vi.fn(
      (_path: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          const signal = init.signal!;
          if (signal.aborted) reject(signal.reason);
          else signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
    );
    vi.stubGlobal("fetch", fetch);
    return fetch;
  }

  it("三十秒后取消悬挂请求，返回清晰中文超时错误且清理计时器", async () => {
    const fetch = pendingFetch();
    const result = api.document("synthetic").catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(30000);
    const error = await result;
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 408, message: expect.stringContaining("读取译文超时") });
    expect(fetch.mock.calls[0]![1].signal!.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("页面离开时取消请求，不将主动取消误报为超时", async () => {
    pendingFetch();
    const controller = new AbortController();
    const result = api.document("synthetic", controller.signal).catch((error: unknown) => error);
    controller.abort();
    const error = await result;
    expect(error).toBe(controller.signal.reason);
    expect(error).not.toBeInstanceOf(ApiError);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("正常返回后清理计时器，后续不会取消已经完成的请求", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ segments: [] }) });
    vi.stubGlobal("fetch", fetch);
    await expect(api.document("synthetic")).resolves.toEqual({ segments: [] });
    await vi.advanceTimersByTimeAsync(30000);
    expect(fetch.mock.calls[0]![1].signal.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});
