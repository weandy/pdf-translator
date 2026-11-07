import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setAuthHeaders } from "@/shared/api/client";

describe("统计请求的数据范围", () => {
  afterEach(() => {
    setAuthHeaders(() => ({}));
    vi.unstubAllGlobals();
  });

  it("访问记录只发送随机标识，不附带个人模型凭据", async () => {
    setAuthHeaders(() => ({ "X-LLM-Key": "synthetic-key", "X-Parser-Key": "synthetic-token" }));
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 204 });
    vi.stubGlobal("fetch", fetch);
    const body = {
      event_id: "00000000-0000-4000-8000-000000000001",
      visitor_id: "00000000-0000-4000-8000-000000000002",
    };
    await api.recordVisit(body);
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("/api/stats/visits");
    expect(init.headers).toEqual({ Accept: "application/json", "Content-Type": "application/json" });
    expect(JSON.parse(init.body)).toEqual(body);
  });

  it("公开汇总查询也不发送个人模型凭据", async () => {
    setAuthHeaders(() => ({ "X-LLM-Key": "synthetic-key" }));
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ total: {} }) });
    vi.stubGlobal("fetch", fetch);
    await api.stats();
    expect(fetch.mock.calls[0]![1].headers).toEqual({ Accept: "application/json" });
  });
});
