import { describe, expect, it } from "vitest";

import { parseRich, splitByParts, tokenize } from "@/shared/text/split";

describe("splitByParts", () => {
  it("单块不拆", () => {
    expect(splitByParts("全部译文", [10])).toEqual(["全部译文"]);
  });

  it("按原文长度比例在标点处拆开，拼回后与原文一致", () => {
    const text = "第一部分的内容比较长，一直写到这里。第二部分短。";
    const parts = splitByParts(text, [30, 10]);
    expect(parts).toHaveLength(2);
    expect(parts.join("")).toBe(text);
    expect(parts[0]?.endsWith("。") || parts[0]?.endsWith("，")).toBe(true);
  });

  it("不会把公式占位符拆开", () => {
    const text = "甲".repeat(9) + "<m12/>" + "乙".repeat(9);
    for (const parts of [splitByParts(text, [10, 10]), splitByParts(text, [11, 9])]) {
      expect(parts.join("")).toBe(text);
      for (const p of parts) expect(p.match(/<m\d*$/)).toBeNull();
    }
  });

  it("三块：每块都分到内容", () => {
    const text = "一，二，三，四，五，六，七，八，九。";
    const parts = splitByParts(text, [6, 6, 6]);
    expect(parts).toHaveLength(3);
    expect(parts.join("")).toBe(text);
    expect(parts.every((p) => p.length > 0)).toBe(true);
  });
});

describe("tokenize", () => {
  it("切出文字与公式", () => {
    expect(tokenize("设<m1/>为函数，<m2/>")).toEqual([
      { kind: "text", text: "设" },
      { kind: "formula", key: "m1" },
      { kind: "text", text: "为函数，" },
      { kind: "formula", key: "m2" },
    ]);
  });
});

describe("parseRich", () => {
  it("解析上下标标签", () => {
    expect(parseRich("Ashish<sup>∗</sup> 与 d<sub>model</sub>")).toEqual([
      "Ashish",
      { tag: "sup", children: ["∗"] },
      " 与 d",
      { tag: "sub", children: ["model"] },
    ]);
  });

  it("白名单外的标签与不配对的结束标签不生成元素", () => {
    expect(parseRich("a<script>x</script>b</sup>c")).toEqual(["a<script>x</script>b", "c"]);
  });
});
