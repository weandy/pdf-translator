import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi, type Mock, type MockInstance } from "vitest";

import { useJob } from "@/entities/job/model";
import { api, ApiError, subscribeJob, type Job } from "@/shared/api/client";

vi.mock("@/shared/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/shared/api/client")>();
  return { ...original, subscribeJob: vi.fn() };
});

const ID = "0000000000000001";
const OTHER_ID = "0000000000000002";

function job(status: Job["status"], id = ID, progress = 0.5): Job {
  return {
    id,
    filename: "synthetic.pdf",
    status,
    stage: status === "done" ? "done" : "translate",
    progress,
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
    created_at: "2026-01-01T00:00:00Z",
    stage_started_at: null,
    expires_at: null,
  };
}

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("任务详情状态同步", () => {
  let root: Root;
  let current: ReturnType<typeof useJob>;
  let onEvent: (value: Job) => void;
  let onStreamError: (() => void) | undefined;
  let unsubscribe: Mock<() => void>;
  let requests: MockInstance<typeof api.job>;

  function Probe({ id }: { id: string }) {
    current = useJob(id);
    return null;
  }

  async function render(id = ID) {
    await act(async () => root.render(<Probe id={id} />));
  }

  async function advance(ms: number) {
    await act(async () => vi.advanceTimersByTimeAsync(ms));
  }

  beforeAll(() => {
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  });
  afterAll(() => {
    Reflect.deleteProperty(globalThis, "IS_REACT_ACT_ENVIRONMENT");
  });
  beforeEach(() => {
    vi.useFakeTimers();
    root = createRoot(document.createElement("div"));
    unsubscribe = vi.fn<() => void>();
    requests = vi.spyOn(api, "job").mockResolvedValue(job("running"));
    vi.mocked(subscribeJob)
      .mockReset()
      .mockImplementation((_id, onJob, onError) => {
        onEvent = onJob;
        onStreamError = onError;
        return unsubscribe;
      });
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("首次立即查询，即使 SSE 一直没有消息也能打开任务", async () => {
    await render();
    expect(requests).toHaveBeenCalledTimes(1);
    expect(current.job?.status).toBe("running");
  });

  it("SSE 没有报错但漏掉完成事件时，三秒内自动显示完成", async () => {
    await render();
    await act(async () => onEvent(job("running")));
    requests.mockResolvedValue(job("done", ID, 1));
    await advance(3000);
    expect(current.job?.status).toBe("done");
    expect(unsubscribe).toHaveBeenCalled();
    const count = requests.mock.calls.length;
    await advance(30000);
    expect(requests).toHaveBeenCalledTimes(count);
  });

  it("首次查询暂时失败后持续重试，成功时自动清除错误", async () => {
    requests.mockRejectedValueOnce(new Error("暂时断网")).mockResolvedValue(job("done"));
    await render();
    expect(current.error).toContain("暂时断网");
    await advance(3000);
    expect(current.job?.status).toBe("done");
    expect(current.error).toBeNull();
  });

  it("已有进度时查询短暂失败，保留进度并继续恢复", async () => {
    await render();
    requests.mockRejectedValueOnce(new Error("暂时断网")).mockResolvedValue(job("done"));
    await advance(3000);
    expect(current.job?.status).toBe("running");
    expect(current.error).toBeNull();
    await advance(3000);
    expect(current.job?.status).toBe("done");
  });

  it("GET 发出后收到更新的 SSE，不允许旧 GET 覆盖新进度", async () => {
    const oldRequest = pending<Job>();
    requests.mockReturnValueOnce(oldRequest.promise);
    await render();
    await act(async () => onEvent(job("running", ID, 0.9)));
    await act(async () => oldRequest.resolve(job("running", ID, 0.1)));
    expect(current.job?.progress).toBe(0.9);
  });

  it("收到完成推送后取消查询，晚到的 running 不会覆盖完成状态", async () => {
    const oldRequest = pending<Job>();
    requests.mockReturnValueOnce(oldRequest.promise);
    await render();
    const signal = requests.mock.calls[0]?.[1];
    await act(async () => onEvent(job("done")));
    expect(signal?.aborted).toBe(true);
    await act(async () => oldRequest.resolve(job("running")));
    await act(async () => onEvent(job("running")));
    expect(current.job?.status).toBe("done");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("SSE 报错后立即查询，不必等下一轮定时器", async () => {
    await render();
    requests.mockResolvedValue(job("done"));
    await act(async () => onStreamError?.());
    expect(current.job?.status).toBe("done");
  });

  it("查询悬挂十秒后取消并重试，不会永久停在正在打开", async () => {
    const hungRequest = pending<Job>();
    requests.mockReturnValueOnce(hungRequest.promise).mockResolvedValue(job("done"));
    await render();
    const signal = requests.mock.calls[0]?.[1];
    await advance(10000);
    expect(signal?.aborted).toBe(true);
    expect(current.error).toContain("超时");
    await advance(3000);
    expect(current.job?.status).toBe("done");
    await act(async () => hungRequest.resolve(job("running")));
    expect(current.job?.status).toBe("done");
  });

  it.each(["visibilitychange", "online"])("收到 %s 后立即校准状态", async (event) => {
    await render();
    requests.mockResolvedValue(job("done"));
    await act(async () => {
      (event === "visibilitychange" ? document : window).dispatchEvent(new Event(event));
    });
    expect(current.job?.status).toBe("done");
  });

  it("手动刷新能清除旧错误并重新建立订阅", async () => {
    requests.mockRejectedValueOnce(new Error("暂时断网"));
    await render();
    expect(current.error).not.toBeNull();
    requests.mockResolvedValue(job("done"));
    await act(async () => current.refresh());
    expect(current.error).toBeNull();
    expect(current.job?.status).toBe("done");
    expect(subscribeJob).toHaveBeenCalledTimes(2);
  });

  it("编辑后刷新摘要保留已打开的译文，返回后更新费用", async () => {
    const completed = job("done");
    requests.mockResolvedValueOnce(completed);
    await render();
    const refreshed = pending<Job>();
    requests.mockReturnValueOnce(refreshed.promise);
    await act(async () => current.refresh());
    expect(current.job).toEqual(completed);
    await act(async () => refreshed.resolve({ ...completed, cost_cny: 0.123 }));
    expect(current.job?.cost_cny).toBe(0.123);
  });

  it("切换任务时忽略上一任务迟到的请求和推送", async () => {
    const oldRequest = pending<Job>();
    requests.mockReturnValueOnce(oldRequest.promise);
    await render();
    const oldEvent = onEvent;
    const oldSignal = requests.mock.calls[0]?.[1];
    requests.mockResolvedValue(job("running", OTHER_ID));
    await render(OTHER_ID);
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => {
      oldEvent(job("done"));
      oldRequest.resolve(job("done"));
    });
    expect(current.job?.id).toBe(OTHER_ID);
    expect(current.job?.status).toBe("running");
  });

  it("离开任务时关闭订阅、取消请求并移除所有重试", async () => {
    const oldRequest = pending<Job>();
    requests.mockReturnValueOnce(oldRequest.promise);
    await render();
    const signal = requests.mock.calls[0]?.[1];
    await act(async () => root.render(null));
    expect(signal?.aborted).toBe(true);
    expect(unsubscribe).toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
      oldRequest.resolve(job("running"));
    });
    await advance(30000);
    expect(requests).toHaveBeenCalledTimes(1);
  });

  it("任务已过期时显示错误并停止重复查询", async () => {
    requests.mockRejectedValue(new ApiError(404, "任务不存在"));
    await render();
    expect(current.error).toContain("任务不存在");
    await advance(30000);
    expect(requests).toHaveBeenCalledTimes(1);
    expect(unsubscribe).toHaveBeenCalled();
  });
});
