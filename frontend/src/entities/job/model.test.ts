import { beforeEach, describe, expect, it } from "vitest";

import { forgetJob, myJobIds, rememberJob, restoreMyJobs } from "@/entities/job/model";

describe("我的任务", () => {
  beforeEach(() => myJobIds().forEach(forgetJob));

  it("最新的排在前面、不重复，可以移除", () => {
    rememberJob("0000000000000001");
    rememberJob("0000000000000002");
    rememberJob("0000000000000001");
    expect(myJobIds()).toEqual(["0000000000000001", "0000000000000002"]);
    forgetJob("0000000000000001");
    expect(myJobIds()).toEqual(["0000000000000002"]);
  });

  it("忽略格式不对的 ID", () => {
    rememberJob("0000000000000002");
    rememberJob("../../etc");
    expect(myJobIds()).toEqual(["0000000000000002"]);
  });

  it("移出批次的成员在后续上传达到 30 篇时仍能出现在我的任务", () => {
    const first = "0000000000000000";
    const second = "0000000000000001";
    rememberJob(first);
    restoreMyJobs([first, second, first]);
    expect(myJobIds()).toEqual([first, second]);
    for (let index = 10; index < 45; index += 1) {
      rememberJob(index.toString(16).padStart(16, "0"));
    }
    expect(myJobIds()).toHaveLength(32);
    expect(myJobIds().slice(-2)).toEqual([first, second]);
    forgetJob(first);
    expect(myJobIds()).not.toContain(first);
    expect(myJobIds()).toHaveLength(31);
  });
});
