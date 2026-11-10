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
    generated_at: "2026-11-07T06:32:10Z",
    period: { start_date: "2026-11-01", end_date: "2026-11-07", counts: { ...zero, visitors: 2 } },
    today: zero,
    daily: Array.from({ length: 7 }, (_, index) => ({ ...zero, date: `2026-11-0${index + 1}`, available: true })),
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
    expect(host.textContent).not.toContain("987,654");
    expect(host.textContent).not.toContain("30 天");
    expect(host.textContent).toContain("0次");
    expect(host.textContent).toContain("访问浏览器");
    expect(host.textContent).toContain("数据更新于 14:32:10");
    expect(host.querySelectorAll("dd")).toHaveLength(10);
  });

  it("图表每天直接显示日期数值，点按与方向键可定位完整数据", async () => {
    vi.spyOn(api, "stats").mockResolvedValue({
      ...empty,
      daily: empty.daily.map((day, index) => ({ ...day, page_views: index + 1 })),
    });
    await render();
    expect(host.querySelectorAll("svg text")).toHaveLength(14);
    const buttons = host.querySelectorAll<HTMLButtonElement>(".stats-day-buttons button");
    expect(buttons).toHaveLength(7);
    expect(buttons[6]?.getAttribute("aria-pressed")).toBe("true");
    await act(async () => buttons[0]?.click());
    expect(host.querySelector(".stats-selection")?.textContent).toContain("2026-11-01 · 访问次数：1");
    await act(async () =>
      buttons[0]?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })),
    );
    expect(buttons[1]?.getAttribute("aria-pressed")).toBe("true");
  });

  it("启用前的日期显示尚未启用，不伪装成观测到的零", async () => {
    vi.spyOn(api, "stats").mockResolvedValue({
      ...empty,
      daily: empty.daily.map((day, index) => ({ ...day, available: index > 0 })),
    });
    await render();
    const first = host.querySelector<HTMLButtonElement>(".stats-day-buttons button")!;
    await act(async () => first.click());
    expect(host.querySelector(".stats-selection")?.textContent).toContain("尚未启用");
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
