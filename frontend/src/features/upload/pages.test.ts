import { describe, expect, it } from "vitest";

import { parsePageRange } from "@/features/upload/pages";

describe("parsePageRange", () => {
  it("1 起的范围转成 0 起页码，去重排序", () => {
    expect(parsePageRange("", 10)).toBeNull();
    expect(parsePageRange("1-3, 5", 10)).toEqual([0, 1, 2, 4]);
    expect(parsePageRange("5，2－3、2", 10)).toEqual([1, 2, 4]); // 中文逗号、全角短横、顿号
    expect(parsePageRange("8 ~ 10", 10)).toEqual([7, 8, 9]);
  });

  it("格式不对或超出总页数给出说明", () => {
    expect(parsePageRange("abc", 10)).toEqual({ error: "看不懂“abc”，请写成 1-10, 15 这样" });
    expect(parsePageRange("3-1", 10)).toEqual({ error: "“3-1”不是有效的范围" });
    expect(parsePageRange("9-12", 10)).toEqual({ error: "这份 PDF 只有 10 页" });
    expect(parsePageRange("0", 10)).toEqual({ error: "“0”不是有效的范围" });
  });
});
