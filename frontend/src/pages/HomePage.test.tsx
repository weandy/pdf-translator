import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { HomePage } from "@/pages/HomePage";
import { api } from "@/shared/api/client";

const fixture = vi.hoisted(() => ({ jobs: [] as { id: string; status: string; filename: string; cost_cny: null }[] }));
vi.mock("@/entities/job/model", () => ({
  useMyJobs: () => ({ data: { jobs: fixture.jobs, warning: null } }),
  deleteJob: vi.fn(),
  MY_JOBS_QUERY: ["my-jobs"],
  STAGE_LABELS: {},
}));
vi.mock("@/entities/batch/model", () => ({
  readBatches: () => [{ id: "batch", name: "合成批次", jobIds: ["done", "running"], createdAt: "2026-11-18" }],
  rememberBatch: vi.fn(),
  removeBatch: vi.fn(),
}));
vi.mock("@/features/upload/UploadPanel", () => ({
  UploadPanel: () => <button>选择英文论文 PDF</button>,
}));

describe("首页内容", () => {
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  it("保留上传入口，不展示或查询站点统计", async () => {
    fixture.jobs = [];
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

  it("任务和批次仅为已完成论文提供双语默认下载选择", async () => {
    fixture.jobs = [
      { id: "done", filename: "合成完成.pdf", status: "done", cost_cny: null },
      { id: "running", filename: "合成处理中.pdf", status: "running", cost_cny: null },
    ];
    const host = document.createElement("div");
    const root = createRoot(host);
    const client = new QueryClient();
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
      const choices = host.querySelectorAll<HTMLSelectElement>(".download-choice select");
      expect(choices).toHaveLength(2);
      expect([...choices].every((select) => select.value === "dual")).toBe(true);
      expect(host.querySelector(".batch-items .download-choice a")?.getAttribute("href")).toContain(
        "mode=dual&side=right",
      );
    } finally {
      await act(async () => root.unmount());
      client.clear();
      fixture.jobs = [];
    }
  });
});
