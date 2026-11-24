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

  it("支持概况、配置读写、PUT 分区保存、连接测试、版本回滚与术语库重载", async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);

    await adminApi.overview();
    await adminApi.getConfig();
    await adminApi.updateConfig(
      "pricing",
      {
        expected_revision: 3,
        models: [{ model: "deepseek-flash", cache_hit: 0.02, cache_miss: 1, output: 4, peak_factor: 2 }],
      },
      "synthetic-csrf",
    );
    await adminApi.testConnection({ target: "mineru", base_url: "https://mineru.net" }, "synthetic-csrf");
    await adminApi.listRevisions({ limit: 15, offset: 5 });
    await adminApi.rollbackConfig({ target_revision: 2, expected_revision: 3 }, "synthetic-csrf");
    await adminApi.reloadGlossary("synthetic-csrf");

    expect(fetch.mock.calls.map(([path, init]) => [path, (init as RequestInit).method])).toEqual([
      ["/api/admin/overview", "GET"],
      ["/api/admin/config", "GET"],
      ["/api/admin/config/pricing", "PUT"],
      ["/api/admin/config/test", "POST"],
      ["/api/admin/config/revisions?limit=15&offset=5", "GET"],
      ["/api/admin/config/rollback", "POST"],
      ["/api/admin/glossary/reload", "POST"],
    ]);

    for (const index of [2, 3, 5, 6]) {
      const headers = new Headers(fetch.mock.calls[index]![1].headers);
      expect(headers.get("X-CSRF-Token")).toBe("synthetic-csrf");
      expect(headers.get("Content-Type")).toBe("application/json");
      expect(headers.has("X-LLM-Key")).toBe(false);
    }
    expect(fetch.mock.calls[2]![1].body).toContain('"expected_revision":3');
  });

  it("409 冲突、413 过大、429 限流与 400 校验返回安全中文提示且不泄漏原始输入", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: "raw-secret-token-should-not-leak" }), { status: 409 }),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: "raw-secret-body" }), { status: 413 }),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: "raw-rate-limit" }), {
            status: 429,
            headers: { "Retry-After": "30" },
          }),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              detail: "修改服务地址或协议时必须同时更换或禁用原密钥，避免将旧密钥发送到新地址",
            }),
            { status: 400 },
          ),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ detail: "unexpected-raw-user-secret-xyz" }), { status: 400 }),
        ),
    );

    const err409 = await adminApi
      .updateConfig("glossary", { expected_revision: 1, sources: ["d2l"] }, "csrf")
      .catch((r: unknown) => r);
    expect(err409).toBeInstanceOf(AdminApiError);
    expect((err409 as AdminApiError).status).toBe(409);
    expect((err409 as AdminApiError).message).toContain("已在其他窗口更新");
    expect((err409 as AdminApiError).message).not.toContain("raw-secret-token");

    const err413 = await adminApi.reloadGlossary("csrf").catch((r: unknown) => r);
    expect((err413 as AdminApiError).status).toBe(413);
    expect((err413 as AdminApiError).message).toContain("大小限制");

    const err429 = await adminApi.reloadGlossary("csrf").catch((r: unknown) => r);
    expect((err429 as AdminApiError).status).toBe(429);
    expect((err429 as AdminApiError).retryAfter).toBe("30");
    expect((err429 as AdminApiError).message).toContain("过于频繁");

    const err400Known = await adminApi.reloadGlossary("csrf").catch((r: unknown) => r);
    expect((err400Known as AdminApiError).status).toBe(400);
    expect((err400Known as AdminApiError).message).toContain("更换或禁用原密钥");

    const err400Unknown = await adminApi.reloadGlossary("csrf").catch((r: unknown) => r);
    expect((err400Unknown as AdminApiError).status).toBe(400);
    expect((err400Unknown as AdminApiError).message).not.toContain("unexpected-raw-user-secret-xyz");
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
