import { describe, expect, it } from "vitest";

import { formatDuration } from "@/shared/time/elapsed";

describe("formatDuration", () => {
  it("按量级显示", () => {
    expect(formatDuration(-5)).toBe("0 秒");
    expect(formatDuration(42_000)).toBe("42 秒");
    expect(formatDuration(200_000)).toBe("3 分 20 秒");
    expect(formatDuration(3_900_000)).toBe("1 小时 5 分");
  });
});
