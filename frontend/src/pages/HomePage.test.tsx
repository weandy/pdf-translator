import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { HomePage } from "@/pages/HomePage";
import { api } from "@/shared/api/client";

vi.mock("@/entities/job/model", () => ({
  useMyJobs: () => ({ data: [] }),
  deleteJob: vi.fn(),
  MY_JOBS_QUERY: ["my-jobs"],
  STAGE_LABELS: {},
}));
vi.mock("@/features/upload/UploadPanel", () => ({
  UploadPanel: () => <button>选择英文论文 PDF</button>,
}));

describe("首页内容", () => {
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  it("保留上传入口，不展示或查询站点统计", async () => {
    const host = document.createElement("div");
    const root = createRoot(host);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const stats = vi.spyOn(api, "stats").mockRejectedValue(new Error("首页不应查询统计"));
    try {
      await act(async () =>
        root.render(
          <QueryClientProvider client={client}>
            <MemoryRouter>
              <HomePage openSettings={vi.fn()} />
            </MemoryRouter>
          </QueryClientProvider>,
        ),
      );
      expect(host.textContent).toContain("选择英文论文 PDF");
      expect(host.textContent).not.toContain("本站累计使用");
      expect(host.textContent).not.toContain("查看统计");
      expect(host.querySelector(".stats-metrics")).toBeNull();
      expect(stats).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
      client.clear();
      vi.restoreAllMocks();
    }
  });
});
