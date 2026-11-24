import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetJob, myJobIds, rememberJob, useMyJobs } from "@/entities/job/model";
import { api, ApiError, type Job } from "@/shared/api/client";

const DONE = "0000000000000001";
const MISSING = "0000000000000002";
const GONE = "0000000000000003";
const TEMPORARY = "0000000000000004";
const PENDING = "0000000000000005";

function job(id: string): Job {
  return {
    id,
    filename: "合成论文.pdf",
    status: "done",
    stage: "done",
    progress: 1,
    message: "",
    error: "",
    page_count: 1,
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
    created_at: "2026-01-01T00:00:00Z",
    stage_started_at: null,
    expires_at: null,
  };
}

describe("首页任务查询", () => {
  let root: Root;
  let client: QueryClient;
  let current: ReturnType<typeof useMyJobs>;

  function Probe({ ids }: { ids: string[] }) {
    current = useMyJobs(ids);
    return null;
  }

  async function advance(ms: number) {
    await act(async () => vi.advanceTimersByTimeAsync(ms));
  }

  beforeAll(() => Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true }));
  afterAll(() => Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT"));

  beforeEach(() => {
    vi.useFakeTimers();
    myJobIds().forEach(forgetJob);
    root = createRoot(document.createElement("div"));
    client = new QueryClient();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    client.clear();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("独立查询批次成员，只有 404 和 410 算不可用，瞬时错误会重试", async () => {
    rememberJob(DONE);
    rememberJob(MISSING);
    let recovered = false;
    const lookup = vi.spyOn(api, "job").mockImplementation(async (id) => {
      if (id === MISSING) throw new ApiError(404, "任务不存在");
      if (id === GONE) throw new ApiError(410, "任务已过期");
      if (id === TEMPORARY && !recovered) throw new ApiError(503, "暂时无法查询");
      if (id === PENDING) return new Promise<Job>(() => {});
      return job(id);
    });

    await act(async () =>
      root.render(
        <QueryClientProvider client={client}>
          <Probe ids={[DONE, GONE, TEMPORARY, PENDING, "无效 ID"]} />
        </QueryClientProvider>,
      ),
    );
    await advance(1);

    expect(lookup.mock.calls.map(([id]) => id)).toEqual([MISSING, DONE, GONE, TEMPORARY, PENDING]);
    expect(myJobIds()).toEqual([DONE]);
    expect(current.isPending).toBe(true);
    expect(current.data.jobs.map(({ id }) => id)).toEqual([DONE]);
    expect(current.data.unavailableIds).toEqual([MISSING, GONE]);
    expect(current.data.failedIds).toEqual([TEMPORARY]);

    recovered = true;
    await advance(3000);

    expect(lookup.mock.calls.slice(5).map(([id]) => id)).toEqual([TEMPORARY]);
    expect(current.data.jobs.map(({ id }) => id)).toEqual([DONE, TEMPORARY]);
    expect(current.data.unavailableIds).toEqual([MISSING, GONE]);
    expect(current.data.failedIds).toEqual([]);
  });
});
