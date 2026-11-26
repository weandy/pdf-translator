import { beforeEach, describe, expect, it } from "vitest";

import { pruneBatches, readBatches, rememberBatch, removeBatch } from "./model";

describe("本机批次", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("保存多文件批次并支持移除分组", () => {
    const batch = rememberBatch(["job-a", "job-a", "job-b"], "测试批次");
    expect(batch?.jobIds).toEqual(["job-a", "job-b"]);
    expect(readBatches()[0]?.name).toBe("测试批次");
    removeBatch(batch?.id ?? "");
    expect(readBatches()).toEqual([]);
  });

  it("单个任务不建立批次", () => {
    expect(rememberBatch(["job-only"])).toBeNull();
  });

  it("自动清理失效批次：全组过期直接移除，剩一篇升格为单篇，剩多篇保留剩余成员", () => {
    rememberBatch(["dead-1", "dead-2"], "全组失效");
    rememberBatch(["alive-only", "dead-3"], "仅剩一篇");
    rememberBatch(["keep-1", "dead-4", "keep-2"], "保留多篇");

    const result = pruneBatches(["dead-1", "dead-2", "dead-3", "dead-4"]);
    expect(result.changed).toBe(true);
    expect(result.promotedJobIds).toEqual(["alive-only"]);
    expect(result.batches.map((batch) => batch.jobIds)).toEqual([["keep-1", "keep-2"]]);
    expect(readBatches().map((batch) => batch.jobIds)).toEqual([["keep-1", "keep-2"]]);

    const noop = pruneBatches(["nonexistent"]);
    expect(noop.changed).toBe(false);
    expect(noop.promotedJobIds).toEqual([]);
  });
});
