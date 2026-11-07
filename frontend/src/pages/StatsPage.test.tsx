import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { StatsPage } from "@/pages/StatsPage";
import { api, type SiteStats } from "@/shared/api/client";

describe("最近七天统计页面", () => {
  let root: Root;
  let host: HTMLDivElement;
  let client: QueryClient;
  const zero = { page_views: 0, visitors: 0, jobs_submitted: 0, jobs_completed: 0, pages_submitted: 0 };
  const empty: SiteStats = {
    since: "2026-10-01T00:00:00Z",
    timezone: "Asia/Shanghai",
    total: { ...zero, page_views: 987654 },
    today: zero,
    daily: Array.from({ length: 7 }, (_, index) => ({ ...zero, date: `2026-11-0${index + 1}` })),
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
            <StatsPage />
          </MemoryRouter>
        </QueryClientProvider>,
      ),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
  }

  it("只请求七天并渲染七条每日记录，不展示历史累计量", async () => {
    vi.spyOn(api, "stats").mockResolvedValue(empty);
    await render();
    expect(api.stats).toHaveBeenCalledWith(7, expect.any(AbortSignal));
    expect(host.querySelectorAll("tbody tr")).toHaveLength(7);
    expect(host.querySelectorAll("svg rect")).toHaveLength(7);
    expect(host.textContent).toContain("最近 7 天");
    expect(host.textContent).toContain("历史汇总数据长期保留");
    expect(host.textContent).not.toContain("累计");
    expect(host.textContent).not.toContain("987,654");
    expect(host.textContent).not.toContain("30 天");
    expect(host.textContent).toContain("0次");
  });

  it("请求尚未完成时展示加载，不以零代替未知数据", async () => {
    vi.spyOn(api, "stats").mockImplementation(() => new Promise(() => {}));
    await render();
    expect(host.textContent).toContain("正在加载统计");
    expect(host.querySelectorAll("dd")).toHaveLength(0);
  });

  it("请求失败时展示错误和重试入口", async () => {
    vi.spyOn(api, "stats").mockRejectedValue(new Error("暂时不可用"));
    await render();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1100));
    });
    expect(host.textContent).toContain("统计暂时不可用");
    expect(host.textContent).toContain("重新加载");
    expect(host.querySelectorAll("dd")).toHaveLength(0);
  });
});
