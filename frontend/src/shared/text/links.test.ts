import { describe, expect, it } from "vitest";

import { countCompact, linkRanges } from "@/shared/text/links";

describe("linkRanges", () => {
  it("忽略空白找第 n 处", () => {
    // 第二处 22 才是引用
    expect(linkRanges(["我们用 22 层网络，见 [22, 21]。"], [{ text: "22", nth: 1 }])).toEqual([
      [{ start: 14, end: 16, link: 0 }],
    ]);
    // 中西文间距（阅读器自动加的空格）不影响
    expect(linkRanges(["Vaswani et al., 2017"], [{ text: "Vaswanietal", nth: 0 }])[0]).toEqual([
      { start: 0, end: 13, link: 0 },
    ]);
  });

  it("跨两段文字时各包一部分，找不到或已被占用的跳过", () => {
    expect(linkRanges(["见图", " 2 与表"], [{ text: "图2", nth: 0 }])).toEqual([
      [{ start: 1, end: 2, link: 0 }],
      [{ start: 1, end: 2, link: 0 }],
    ]);
    expect(
      linkRanges(
        ["[12]"],
        [
          { text: "99", nth: 0 },
          { text: "12", nth: 0 },
          { text: "12", nth: 0 },
        ],
      ),
    ).toEqual([[{ start: 1, end: 3, link: 1 }]]);
  });

  it("countCompact", () => {
    expect(countCompact("图 1：图 1 的说明", "图1")).toBe(2);
    expect(countCompact("abc", "")).toBe(0);
  });
});
