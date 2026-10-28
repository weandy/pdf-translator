import { afterEach, describe, expect, it, vi } from "vitest";

import { readJSON, remove, writeJSON } from "@/shared/storage/local";

afterEach(() => {
  vi.restoreAllMocks();
  remove("k");
});

describe("local storage wrapper", () => {
  it("写入后能读回", () => {
    writeJSON("k", { model: "deepseek-flash" });
    expect(readJSON("k", null)).toEqual({ model: "deepseek-flash" });
  });

  it("没有值时返回默认值", () => {
    expect(readJSON("missing", 42)).toBe(42);
  });

  it("localStorage 不可用时退回内存，不抛异常", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    writeJSON("k", "v");
    expect(readJSON("k", "")).toBe("v");
  });
});
