import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { STATS_QUERY, usePageVisit } from "@/entities/stats/model";
import { api } from "@/shared/api/client";
import { readJSON, remove } from "@/shared/storage/local";

const VISITOR_KEY = "pdftr.stats.visitor.v1";

describe("匿名页面访问", () => {
  let root: Root;
  let client: QueryClient;

  function Probe({ navigationKey }: { navigationKey: string }) {
    usePageVisit(navigationKey);
    return <p>主页面正常工作</p>;
  }

  async function render(key = "first", strict = false) {
    await act(async () => {
      const probe = <Probe navigationKey={key} />;
      root.render(
        <QueryClientProvider client={client}>{strict ? <StrictMode>{probe}</StrictMode> : probe}</QueryClientProvider>,
      );
    });
  }

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    remove(VISITOR_KEY);
    vi.useFakeTimers();
    root = createRoot(document.createElement("div"));
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.spyOn(api, "recordVisit").mockResolvedValue(undefined);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    remove(VISITOR_KEY);
    vi.useRealTimers();
  });

  it("导航时记录访问，普通重渲染不重复发送，浏览器标识保持一致", async () => {
    await render("first");
    await render("first");
    expect(api.recordVisit).toHaveBeenCalledTimes(1);
    await render("second");
    expect(api.recordVisit).toHaveBeenCalledTimes(2);
    const [first, second] = vi.mocked(api.recordVisit).mock.calls.map(([body]) => body);
    expect(first!.visitor_id).toBe(second!.visitor_id);
    expect(first!.event_id).not.toBe(second!.event_id);
    expect(Object.keys(first!).sort()).toEqual(["event_id", "visitor_id"]);
    expect(readJSON(VISITOR_KEY, "")).toBe(first!.visitor_id);
  });

  it("StrictMode 重复 effect 使用同一事件标识，由服务端幂等去重", async () => {
    await render("first", true);
    const calls = vi.mocked(api.recordVisit).mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(1);
    expect(new Set(calls.map(([body]) => body.event_id)).size).toBe(1);
    expect(new Set(calls.map(([body]) => body.visitor_id)).size).toBe(1);
  });

  it("访问记录成功后使概览缓存失效", async () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await render();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: STATS_QUERY });
  });

  it("统计上传失败不会抛出错误或阻止页面", async () => {
    vi.mocked(api.recordVisit).mockRejectedValue(new Error("暂时不可用"));
    await render();
    expect(api.recordVisit).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("localStorage 不可用时仍在内存中复用浏览器标识", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("存储被禁用");
    });
    await render("first");
    await render("second");
    const [first, second] = vi.mocked(api.recordVisit).mock.calls.map(([body]) => body);
    expect(first!.visitor_id).toBe(second!.visitor_id);
  });

  it("HTTP 环境没有 randomUUID 时生成合法 UUID，不影响访问统计", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    await render();
    const body = vi.mocked(api.recordVisit).mock.calls[0]![0];
    expect(body.event_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it("上传悬挂时五秒后取消，离开页面也会取消", async () => {
    vi.mocked(api.recordVisit).mockImplementation(() => new Promise(() => {}));
    await render();
    const signal = vi.mocked(api.recordVisit).mock.calls[0]![1]!;
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(signal.aborted).toBe(true);
    await render("second");
    const second = vi.mocked(api.recordVisit).mock.calls[1]![1]!;
    await act(async () => root.render(null));
    expect(second.aborted).toBe(true);
  });
});
