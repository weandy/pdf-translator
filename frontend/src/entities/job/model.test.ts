import { describe, expect, it } from "vitest";

import { forgetJob, myJobIds, rememberJob } from "@/entities/job/model";

describe("我的任务", () => {
  it("最新的排在前面、不重复，可以移除", () => {
    rememberJob("0000000000000001");
    rememberJob("0000000000000002");
    rememberJob("0000000000000001");
    expect(myJobIds()).toEqual(["0000000000000001", "0000000000000002"]);
    forgetJob("0000000000000001");
    expect(myJobIds()).toEqual(["0000000000000002"]);
  });

  it("忽略格式不对的 ID", () => {
    rememberJob("../../etc");
    expect(myJobIds()).toEqual(["0000000000000002"]);
  });
});
