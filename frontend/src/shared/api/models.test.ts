import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setAuthHeaders } from "@/shared/api/client";

describe("自定义模型查询", () => {
  afterEach(() => {
    setAuthHeaders(() => ({}));
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("只发送本次未保存的设置，不混入已保存的地址、Key 或解析 Token", async () => {
    setAuthHeaders(() => ({
      "X-LLM-Key": "saved-key",
      "X-LLM-Base-URL": "https://saved.example.invalid",
      "X-Parser-Key": "saved-parser-token",
    }));
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ models: ["a-model"] }) });
    vi.stubGlobal("fetch", fetch);
    const headers = { "X-LLM-Protocol": "anthropic", "X-LLM-Key": "draft-key" };
    await expect(api.llmModels(headers)).resolves.toEqual(["a-model"]);
    await api.llmCheck(headers);
    for (const [, init] of fetch.mock.calls) {
      expect(init.headers).toEqual({ Accept: "application/json", ...headers });
    }
  });

  function pendingFetch() {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal!.addEventListener("abort", () => reject(init.signal!.reason), { once: true });
          }),
      ),
    );
  }

  it("查询20秒超时后中止请求并允许用户重试", async () => {
    vi.useFakeTimers();
    pendingFetch();
    const result = api.llmModels({}).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(20000);
    expect(await result).toMatchObject({ status: 408, message: expect.stringContaining("获取模型列表超时") });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("切换配置主动取消时清理超时计时器，不误报超时", async () => {
    vi.useFakeTimers();
    pendingFetch();
    const controller = new AbortController();
    const result = api.llmModels({}, controller.signal).catch((e: unknown) => e);
    controller.abort();
    expect(await result).toBe(controller.signal.reason);
    expect(vi.getTimerCount()).toBe(0);
  });
});
