import { describe, expect, it } from "vitest";

import { balanceRichParts, parseRich, splitByParts, tokenize } from "@/shared/text/split";

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

  it("跨块粗体分别闭合和重开，正文与公式不丢失", () => {
    const text = "<b>" + "甲".repeat(9) + "<m12/>" + "乙".repeat(9) + "</b>普通";
    const parts = splitByParts(text, [10, 10]);
    expect(parts[0]?.startsWith("<b>")).toBe(true);
    expect(parts[0]?.endsWith("</b>")).toBe(true);
    expect(parts[1]?.startsWith("<b>")).toBe(true);
    expect(parts.map((p) => p.replace(/<\/?b>/g, "")).join("")).toBe(text.replace(/<\/?b>/g, ""));
    expect(parts.join("").match(/<m12\/>/g)).toHaveLength(1);
    expect(parts[1]?.endsWith("</b>普通")).toBe(true);
  });
});

describe("balanceRichParts", () => {
  it("跨分片嵌套样式继续生效，结束后不串到普通正文", () => {
    expect(balanceRichParts(["<b>甲<i>乙", "丙</i>丁</b>戊"])).toEqual(["<b>甲<i>乙</i></b>", "<b><i>丙</i>丁</b>戊"]);
    expect(balanceRichParts(["<sup>2", "3</sup>普通"])).toEqual(["<sup>2</sup>", "<sup>3</sup>普通"]);
    expect(balanceRichParts(["<sub>i", "j</sub>普通"])).toEqual(["<sub>i</sub>", "<sub>j</sub>普通"]);
  });

  it("空片段与只有标签的片段不创建空样式节点，未知标签原样保留", () => {
    expect(balanceRichParts(["<b>", "", "甲", "", "</b>", "乙"])).toEqual(["", "", "<b>甲</b>", "", "", "乙"]);
    expect(balanceRichParts(["<script>甲", "乙</script>"])).toEqual(["<script>甲", "乙</script>"]);
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
