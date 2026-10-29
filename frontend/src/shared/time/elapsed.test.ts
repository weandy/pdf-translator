import { describe, expect, it } from "vitest";

import { expiresSoon, formatDuration, formatRemaining } from "@/shared/time/elapsed";

describe("formatDuration", () => {
  it("按量级显示", () => {
    expect(formatDuration(-5)).toBe("0 秒");
    expect(formatDuration(42_000)).toBe("42 秒");
    expect(formatDuration(200_000)).toBe("3 分 20 秒");
    expect(formatDuration(3_900_000)).toBe("1 小时 5 分");
  });
});

describe("formatRemaining", () => {
  it("按小时、分钟显示剩余时间", () => {
    const now = Date.parse("2026-10-29T10:00:00Z");
    expect(formatRemaining("2026-10-30T09:30:00Z", now)).toBe("还剩 23 小时");
    expect(formatRemaining("2026-10-29T10:40:00Z", now)).toBe("还剩 40 分钟");
    expect(formatRemaining("2026-10-29T09:00:00Z", now)).toBe("即将删除");
    expect(expiresSoon("2026-10-29T12:00:00Z", now)).toBe(true);
    expect(expiresSoon("2026-10-30T09:00:00Z", now)).toBe(false);
    expect(expiresSoon(null, now)).toBe(false);
  });
});
