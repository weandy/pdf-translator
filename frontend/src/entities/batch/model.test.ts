import { describe, expect, it } from "vitest";

import { readBatches, rememberBatch, removeBatch } from "./model";

describe("本机批次", () => {
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
});
