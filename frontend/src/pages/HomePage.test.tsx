import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { BatchEntry } from "@/entities/batch/model";
import { HomePage } from "@/pages/HomePage";
import { api, type Job } from "@/shared/api/client";

const fixture = vi.hoisted(() => ({
  jobs: [] as Pick<Job, "id" | "status" | "filename" | "cost_cny">[],
  batches: [] as BatchEntry[],
  unavailableIds: [] as string[],
  failedIds: [] as string[],
  restoreMyJobs: vi.fn(),
}));
vi.mock("@/entities/job/model", () => ({
  useMyJobs: () => ({
    data: {
      jobs: fixture.jobs,
      unavailableIds: fixture.unavailableIds,
      failedIds: fixture.failedIds,
      warning: null,
    },
  }),
  myJobIds: () => [],
  restoreMyJobs: fixture.restoreMyJobs,
  deleteJob: vi.fn(),
  MY_JOBS_QUERY: ["my-jobs"],
  STAGE_LABELS: {},
}));
vi.mock("@/entities/batch/model", () => ({
  readBatches: () => fixture.batches,
  rememberBatch: vi.fn(),
  removeBatch: (id: string) => {
    fixture.batches = fixture.batches.filter((batch) => batch.id !== id);
  },
}));
vi.mock("@/features/upload/UploadPanel", () => ({
  UploadPanel: () => <button>选择英文论文 PDF</button>,
}));

async function renderHome(assertions: (host: HTMLElement) => Promise<void> | void) {
  const host = document.createElement("div");
  const root = createRoot(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
    await assertions(host);
  } finally {
    await act(async () => root.unmount());
    client.clear();
  }
}

describe("首页内容", () => {
  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));
  afterEach(() => {
    fixture.jobs = [];
    fixture.batches = [];
    fixture.unavailableIds = [];
    fixture.failedIds = [];
    fixture.restoreMyJobs.mockClear();
    vi.restoreAllMocks();
  });

  it("保留上传入口，不展示或查询站点统计", async () => {
    const stats = vi.spyOn(api, "stats").mockRejectedValue(new Error("首页不应查询统计"));
    await renderHome((host) => {
      expect(host.textContent).toContain("选择英文论文 PDF");
      expect(host.textContent).not.toContain("本站累计使用");
      expect(host.textContent).not.toContain("查看统计");
      expect(host.querySelector(".stats-metrics")).toBeNull();
      expect(stats).not.toHaveBeenCalled();
    });
  });

  it("批次排在个人任务前，所有已完成的批次成员共用一个下载格式", async () => {
    fixture.batches = [{ id: "batch", name: "合成批次", createdAt: "", jobIds: ["done", "another", "running"] }];
    fixture.jobs = [
      { id: "done", filename: "合成完成.pdf", status: "done", cost_cny: null },
      { id: "another", filename: "合成第二篇.pdf", status: "done", cost_cny: null },
      { id: "running", filename: "合成处理中.pdf", status: "running", cost_cny: null },
      { id: "single", filename: "合成单篇.pdf", status: "done", cost_cny: null },
    ];
    await renderHome(async (host) => {
      expect(host.querySelector(".home-hero")?.nextElementSibling?.classList.contains("batches")).toBe(true);
      const standalone = host.querySelector('[aria-labelledby="jobs-title"]');
      expect(standalone?.querySelectorAll(".job-item")).toHaveLength(1);
      expect(standalone?.textContent).toContain("合成单篇.pdf");
      expect(host.querySelectorAll('a[href="/jobs/done"]')).toHaveLength(1);
      expect(host.querySelectorAll(".batch-card select")).toHaveLength(1);

      const select = host.querySelector<HTMLSelectElement>(".batch-card select")!;
      const downloadUrls = () =>
        [...host.querySelectorAll(".batch-items a[download]")].map((link) => link.getAttribute("href"));
      expect(select.value).toBe("dual");
      expect(downloadUrls()).toEqual([
        "/api/jobs/done/export?mode=dual&side=right",
        "/api/jobs/another/export?mode=dual&side=right",
      ]);
      await act(async () => {
        select.value = "mono";
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      expect(downloadUrls()).toEqual(["/api/jobs/done/export?mode=mono", "/api/jobs/another/export?mode=mono"]);
      expect(standalone?.querySelector("select")?.value).toBe("dual");
    });
  });

  it("批次区分查询中、临时故障和明确不可用，并在移除分组时恢复任务", async () => {
    fixture.batches = [
      { id: "batch", name: "合成批次", createdAt: "", jobIds: ["done", "pending", "offline", "gone"] },
    ];
    fixture.jobs = [{ id: "done", filename: "合成完成.pdf", status: "done", cost_cny: null }];
    fixture.failedIds = ["offline"];
    fixture.unavailableIds = ["gone"];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await renderHome(async (host) => {
      const summary = host.querySelector(".batch-card-info")?.textContent;
      expect(summary).toContain("1 篇查询中");
      expect(summary).toContain("1 篇暂时无法读取");
      expect(summary).toContain("1 篇不可用");
      expect(summary).not.toContain("已结束");
      expect(host.querySelectorAll(".batch-items .unavailable")).toHaveLength(1);
      expect(host.querySelector(".batch-progress")?.getAttribute("aria-valuenow")).toBe("50");

      await act(async () => host.querySelector<HTMLButtonElement>(".batch-card-footer button")?.click());
      expect(fixture.restoreMyJobs).toHaveBeenCalledWith(["done", "pending", "offline"]);
      expect(host.querySelector(".batch-card")).toBeNull();
      expect(host.querySelector('[aria-labelledby="jobs-title"]')?.textContent).toContain("合成完成.pdf");
    });
  });
});
