import { describe, expect, it } from "vitest";

import { isMineruPending } from "./JobProgress";

describe("isMineruPending", () => {
  it("只把等待资源的状态识别为排队", () => {
    expect(isMineruPending("MinerU 排队中（第 1 次尝试）")).toBe(true);
    expect(isMineruPending("MinerU 已提交，等待解析资源")).toBe(true);
    expect(isMineruPending("MinerU 正在解析，第 15/16 页")).toBe(false);
    expect(isMineruPending("MinerU 正在整理解析结果")).toBe(false);
  });
});
