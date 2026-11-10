import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { adminApi, AdminApiError } from "@/shared/api/admin";
import { setAuthHeaders } from "@/shared/api/client";

describe("独立管理客户端", () => {
  beforeEach(() => {
    setAuthHeaders(() => ({ "X-LLM-Key": "synthetic-llm", "X-Parser-Key": "synthetic-parser" }));
  });
  afterEach(() => {
    setAuthHeaders(() => ({}));
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("登录、查询与退出只发送同源 Cookie 和管理 CSRF，不读取翻译凭据", async () => {
    const fetch = vi.fn().mockImplementation(
      async (_path: string, init: RequestInit) =>
        new Response(init.method === "DELETE" ? null : JSON.stringify({ csrf_token: "synthetic-csrf" }), {
          status: init.method === "DELETE" ? 204 : 200,
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await adminApi.login({ password: "synthetic-password" });
    await adminApi.session();
    await adminApi.search({ title_query: "合成标题", page: 1, page_size: 25 }, "synthetic-csrf");
    await adminApi.logout("synthetic-csrf");
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/admin/session",
      "/api/admin/session",
      "/api/admin/submissions/search",
      "/api/admin/session",
    ]);
    for (const [, init] of fetch.mock.calls) {
      expect(init.credentials).toBe("same-origin");
      expect(init.cache).toBe("no-store");
      const headers = new Headers(init.headers);
      expect(headers.has("X-LLM-Key")).toBe(false);
      expect(headers.has("X-Parser-Key")).toBe(false);
      expect(headers.has("Authorization")).toBe(false);
      if (init.method !== "GET") expect(headers.get("Content-Type")).toBe("application/json");
    }
    expect(new Headers(fetch.mock.calls[2]![1].headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(new Headers(fetch.mock.calls[3]![1].headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
    expect(fetch.mock.calls[2]![1].body).toContain("合成标题");
  });

  it("字段校验错误显示固定提示，不透传包含输入的服务端错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [{ loc: ["body", "ip"], msg: "synthetic-sensitive-input" }],
          }),
          { status: 422 },
        ),
      ),
    );
    const error = await adminApi
      .search({ title_query: "", ip: "bad", page: 1, page_size: 25 }, "csrf")
      .catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(AdminApiError);
    expect(error).toMatchObject({ status: 422, fields: { ip: expect.stringContaining("IPv4") } });
    expect(JSON.stringify(error)).not.toContain("synthetic-sensitive-input");
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

  it("20 秒后终止悬挂请求并清理定时器", async () => {
    vi.useFakeTimers();
    const fetch = pendingFetch();
    const result = adminApi.session().catch((reason: unknown) => reason);
    await vi.advanceTimersByTimeAsync(20000);
    expect(await result).toMatchObject({ status: 408 });
    expect(fetch.mock.calls[0]![1].signal!.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("页面取消请求保留取消原因，不误报超时", async () => {
    vi.useFakeTimers();
    pendingFetch();
    const controller = new AbortController();
    const result = adminApi.session(controller.signal).catch((reason: unknown) => reason);
    controller.abort();
    expect(await result).toBe(controller.signal.reason);
    expect(vi.getTimerCount()).toBe(0);
  });
});
