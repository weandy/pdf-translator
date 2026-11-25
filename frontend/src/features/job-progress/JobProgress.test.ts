import { describe, expect, it } from "vitest";

import { isMineruCloudQueued } from "./pending";

describe("isMineruCloudQueued", () => {
  it("只把等待资源的状态识别为排队", () => {
    expect(isMineruCloudQueued("MinerU 排队中（第 1 次尝试）")).toBe(true);
    expect(isMineruCloudQueued("MinerU 已提交，等待解析资源")).toBe(true);
    expect(isMineruCloudQueued("MinerU 正在解析，第 15/16 页")).toBe(false);
    expect(isMineruCloudQueued("MinerU 正在整理解析结果")).toBe(false);
  });
});
