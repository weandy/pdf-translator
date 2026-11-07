import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { StatsSummary } from "@/features/site-stats/StatsSummary";
import { api, type SiteStats } from "@/shared/api/client";

describe("首页统计概览", () => {
  let root: Root;
  let host: HTMLDivElement;
  let client: QueryClient;
  const zero = { page_views: 0, visitors: 0, jobs_submitted: 0, jobs_completed: 0, pages_submitted: 0 };
  const empty: SiteStats = {
    since: "2026-11-07T00:00:00Z",
    timezone: "Asia/Shanghai",
    total: zero,
    today: zero,
    daily: [],
  };
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  beforeEach(() => {
    host = document.createElement("div");
    root = createRoot(host);
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    vi.restoreAllMocks();
  });

  async function render() {
    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <StatsSummary />
          </MemoryRouter>
        </QueryClientProvider>,
      ),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
  }

  it("合法的零计数正常显示，不误判为加载失败", async () => {
    vi.spyOn(api, "stats").mockResolvedValue(empty);
    await render();
    expect(host.querySelectorAll("dd")).toHaveLength(5);
    expect(host.textContent).toContain("0次");
    expect(host.querySelector('a[href="/stats"]')).not.toBeNull();
  });

  it("未完成请求时显示加载状态，不提前显示零", async () => {
    vi.spyOn(api, "stats").mockImplementation(() => new Promise(() => {}));
    await render();
    expect(host.textContent).toContain("正在加载统计");
    expect(host.querySelectorAll("dd")).toHaveLength(0);
  });

  it("请求失败时显示不可用，不编造零计数", async () => {
    vi.spyOn(api, "stats").mockRejectedValue(new Error("连接失败"));
    await render();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1100));
    });
    expect(host.textContent).toContain("统计暂时不可用");
    expect(host.querySelectorAll("dd")).toHaveLength(0);
  });
});
