import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { BatchEntry } from "@/entities/batch/model";
import type { HistoryEntry } from "@/entities/history/model";
import { HomePage } from "@/pages/HomePage";
import { api, type Job } from "@/shared/api/client";

const fixture = vi.hoisted(() => ({
  jobs: [] as Job[],
  batches: [] as BatchEntry[],
  history: [] as HistoryEntry[],
  trackedIds: [] as string[],
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
      warning: fixture.failedIds.length ? "任务列表暂时无法读取，正在重试。" : null,
    },
  }),
  myJobIds: () => [...fixture.trackedIds, ...fixture.jobs.map((job) => job.id)],
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
vi.mock("@/entities/history/model", () => ({
  useHistory: () => fixture.history,
  clearHistory: vi.fn(),
  removeHistory: vi.fn(),
}));

function syntheticJob(id: string, filename: string, status: Job["status"], day: number): Job {
  return {
    id,
    filename,
    status,
    stage: status === "done" ? "done" : "translate",
    progress: status === "done" ? 1 : 0.2,
    message: "",
    error: "",
    page_count: 3,
    options: {
      parser: "mineru-cloud",
      quality: "default",
      include_references: false,
      translate_figures: false,
      cloud_consent: true,
    },
    model: "test",
    cost_cny: null,
    channel_mode: "byok",
    fallback_used: false,
    created_at: `2026-11-${day}T00:00:00Z`,
    stage_started_at: null,
    expires_at: null,
  };
}

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
    fixture.history = [];
    fixture.trackedIds = [];
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

  it("上传区下方用统一卡片按时间展示单篇和多篇，每张卡共用一个下载格式", async () => {
    fixture.batches = [
      { id: "earlier", name: "合成较早批次", createdAt: "2026-11-15T00:00:00Z", jobIds: ["queued", "running"] },
      { id: "batch", name: "合成批次", createdAt: "2026-11-18T00:00:00Z", jobIds: ["done", "another"] },
    ];
    fixture.jobs = [
      syntheticJob("done", "合成完成.pdf", "done", 18),
      syntheticJob("prior", "合成早期单篇.pdf", "queued", 16),
      syntheticJob("queued", "合成排队中.pdf", "queued", 15),
      syntheticJob("another", "合成第二篇.pdf", "done", 18),
      syntheticJob("running", "合成处理中.pdf", "running", 15),
      syntheticJob("single", "合成最新单篇.pdf", "done", 19),
    ];
    await renderHome(async (host) => {
      const tasks = host.querySelector('[aria-labelledby="jobs-title"]')!;
      expect(host.querySelector(".home-hero")?.nextElementSibling).toBe(tasks);
      expect(host.textContent).not.toContain("我的批次");
      const cards = [...tasks.querySelectorAll(".task-list > .batch-card")];
      expect(cards.map((card) => card.querySelector(".batch-card-head h3 time")?.getAttribute("datetime"))).toEqual([
        "2026-11-19T00:00:00Z",
        "2026-11-18T00:00:00Z",
        "2026-11-16T00:00:00Z",
        "2026-11-15T00:00:00Z",
      ]);
      expect(cards.map((card) => card.querySelector(".batch-card-info .hint")?.textContent)).toEqual([
        "可下载 1 / 1 篇",
        "可下载 2 / 2 篇",
        "可下载 0 / 1 篇",
        "可下载 0 / 2 篇",
      ]);
      expect(cards.map((card) => card.querySelectorAll(":scope > .batch-items > .job-item").length)).toEqual([
        1, 2, 1, 2,
      ]);
      expect(cards.map((card) => card.querySelectorAll("select").length)).toEqual([1, 1, 0, 0]);
      expect(cards[0]?.querySelector(".batch-items .status")?.textContent).toBe("可下载");
      expect(cards[1]?.querySelector(".batch-items .status")?.textContent).toBe("可下载");
      expect(cards[0]?.querySelector(".batch-card-footer")).toBeNull();
      expect(host.querySelectorAll('a.name[href="/jobs/done"]')).toHaveLength(1);
      const edits = [...tasks.querySelectorAll<HTMLAnchorElement>("a.edit-translation")];
      expect(edits.map((edit) => edit.getAttribute("href"))).toEqual(["/jobs/single", "/jobs/done", "/jobs/another"]);
      expect(edits.every((edit) => edit.textContent === "编辑译文")).toBe(true);
      for (const edit of edits) {
        expect(edit.nextElementSibling?.querySelector("a[download]")).toBeTruthy();
      }

      const single = cards[0]!;
      const group = cards[1]!;
      const singleFormat = single.querySelector<HTMLSelectElement>(".batch-card-head .batch-format select")!;
      const select = group.querySelector<HTMLSelectElement>(".batch-card-head .batch-format select")!;
      const downloadUrls = () =>
        [...group.querySelectorAll(".batch-items a[download]")].map((link) => link.getAttribute("href"));
      expect(select.value).toBe("dual");
      expect(single.querySelector(".batch-items a[download]")?.getAttribute("href")).toBe(
        "/api/jobs/single/export?mode=dual&side=right",
      );
      expect(downloadUrls()).toEqual([
        "/api/jobs/done/export?mode=dual&side=right",
        "/api/jobs/another/export?mode=dual&side=right",
      ]);
      await act(async () => {
        select.value = "mono";
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      expect(downloadUrls()).toEqual(["/api/jobs/done/export?mode=mono", "/api/jobs/another/export?mode=mono"]);
      expect(singleFormat.value).toBe("dual");
    });
  });

  it("查询故障留在对应任务行，取消分组后成员作为单篇显示", async () => {
    fixture.batches = [
      {
        id: "batch",
        name: "合成批次",
        createdAt: "2026-11-18T00:00:00Z",
        jobIds: ["done", "pending", "offline", "gone"],
      },
    ];
    fixture.jobs = [syntheticJob("done", "合成完成.pdf", "done", 18)];
    fixture.trackedIds = ["offline-solo"];
    fixture.history = ["pending", "offline", "gone", "offline-solo"].map((id) => ({
      id,
      hash: id,
      filename: `${id}.pdf`,
      pages: 1,
      createdAt: "2026-11-18T00:00:00Z",
    }));
    fixture.failedIds = ["offline", "offline-solo"];
    fixture.unavailableIds = ["gone"];
    fixture.restoreMyJobs.mockImplementationOnce((ids: string[]) => {
      fixture.trackedIds.push(...ids);
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await renderHome(async (host) => {
      const group = host.querySelector('a[href="/jobs/done"]')!.closest(".batch-card")!;
      expect(group.querySelector(".batch-card-info")?.textContent).toContain("可下载 1 / 4 篇");
      expect(group.querySelector(".batch-card-info")?.textContent).not.toContain("已结束");
      const rowStatus = (filename: string) =>
        host.querySelector(`[title="${filename}"]`)?.closest(".job-item")?.querySelector(".job-state .status")
          ?.textContent;
      expect(rowStatus("合成完成.pdf")).toBe("可下载");
      expect(rowStatus("pending.pdf")).toBe("查询中");
      expect(rowStatus("offline.pdf")).toBe("暂时无法读取");
      expect(rowStatus("offline-solo.pdf")).toBe("暂时无法读取");
      expect(rowStatus("gone.pdf")).toBe("已过期或已删除");
      expect(host.textContent).not.toContain("任务列表暂时无法读取");

      const ungroup = [...group.querySelectorAll("button")].find((button) => button.textContent === "取消分组")!;
      await act(async () => ungroup.click());
      expect(fixture.restoreMyJobs).toHaveBeenCalledWith(["done", "pending", "offline"]);
      expect(host.querySelector(".batch-card-footer")).toBeNull();
      const cards = [...host.querySelectorAll(".task-list > .batch-card")];
      expect(cards).toHaveLength(4);
      expect(cards.every((card) => card.querySelectorAll(":scope > .batch-items > .job-item").length === 1)).toBe(true);
      expect(host.querySelector('a[href="/jobs/done"]')?.closest(".batch-card")?.textContent).toContain(
        "可下载 1 / 1 篇",
      );
      expect(rowStatus("pending.pdf")).toBe("查询中");
      expect(rowStatus("offline.pdf")).toBe("暂时无法读取");
    });
  });

  it("失败和已取消的任务直接在对应行重试，错误反馈只显示在该行", async () => {
    fixture.batches = [
      { id: "batch", name: "合成批次", createdAt: "2026-11-18T00:00:00Z", jobIds: ["failed", "done"] },
    ];
    fixture.jobs = [
      syntheticJob("failed", "合成失败.pdf", "failed", 18),
      syntheticJob("done", "合成可下载.pdf", "done", 18),
      syntheticJob("cancelled", "合成取消.pdf", "cancelled", 19),
    ];
    const retry = vi.spyOn(api, "retryJob").mockImplementation(async (id) => {
      if (id === "failed") throw new Error("模型配置不可用");
      return syntheticJob("cancelled", "合成取消.pdf", "queued", 19);
    });

    await renderHome(async (host) => {
      const failed = host.querySelector('a[href="/jobs/failed"]')!.closest(".job-item")!;
      const cancelled = host.querySelector('a[href="/jobs/cancelled"]')!.closest(".job-item")!;
      const retryFailed = failed.querySelector<HTMLButtonElement>(".job-retry")!;
      const restartCancelled = cancelled.querySelector<HTMLButtonElement>(".job-retry")!;
      expect(retryFailed.textContent).toBe("重试");
      expect(restartCancelled.textContent).toBe("重新开始");
      expect(host.querySelectorAll(".job-retry")).toHaveLength(2);

      await act(async () => retryFailed.click());
      expect(retry).toHaveBeenNthCalledWith(1, "failed");
      expect(failed.querySelector(".job-action-error")?.textContent).toContain("模型配置不可用");
      expect(cancelled.querySelector(".job-action-error")).toBeNull();

      await act(async () => restartCancelled.click());
      expect(retry).toHaveBeenNthCalledWith(2, "cancelled");
    });
  });

  it("首页底部居中提供 ToS 弹窗与独立条款页链接", async () => {
    await renderHome(async (host) => {
      const footer = host.querySelector(".home-footer")!;
      expect(footer).not.toBeNull();
      expect(footer.querySelector('a[href="/tos"]')).not.toBeNull();
      const trigger = [...footer.querySelectorAll("button")].find((b) =>
        b.textContent?.includes("服务条款与隐私说明"),
      )!;
      expect(trigger).toBeTruthy();

      await act(async () => trigger.click());
      const dialog = host.querySelector('[role="dialog"]')!;
      expect(dialog).not.toBeNull();
      expect(dialog.textContent).toContain("文档解析与翻译数据传输");
      expect(dialog.querySelector('a[href="/tos"]')).not.toBeNull();

      const closeBtn = [...dialog.querySelectorAll("button")].find((b) => b.textContent === "我已了解")!;
      await act(async () => closeBtn.click());
      expect(host.querySelector('[role="dialog"]')).toBeNull();
    });
  });
});
