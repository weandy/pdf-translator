import { describe, expect, it } from "vitest";

import { diffPreview, diffText, type TextChange } from "@/shared/text/diff";

const original = (changes: TextChange[]) =>
  changes
    .filter((c) => c.kind !== "added")
    .map((c) => c.text)
    .join("");
const revised = (changes: TextChange[]) =>
  changes
    .filter((c) => c.kind !== "removed")
    .map((c) => c.text)
    .join("");

describe("译文差异", () => {
  it("只标记中文替换部分，相同文字保持普通样式", () => {
    expect(diffText("采用旧方法求解。", "采用新方法求解。").changes).toEqual([
      { kind: "equal", text: "采用" },
      { kind: "removed", text: "旧" },
      { kind: "added", text: "新" },
      { kind: "equal", text: "方法求解。" },
    ]);
  });

  it("英文按词比较，不把一个替换词拆成零散字母", () => {
    const { changes } = diffText("A fast method.", "A robust method.");
    expect(changes.filter((c) => c.kind !== "equal")).toEqual([
      { kind: "removed", text: "fast" },
      { kind: "added", text: "robust" },
    ]);
  });

  it.each([
    [null, "完整新增"],
    ["完整删除", null],
    [null, null],
    ["原样不变", "原样不变"],
    ["换行\n  与空格", "换行\n 与空格"],
    ["🧪α方案", "🧪β方案"],
  ])("完整插入、删除、空值及特殊字符都能还原两侧：%s → %s", (before, after) => {
    const { changes } = diffText(before, after);
    expect(original(changes)).toBe(before ?? "");
    expect(revised(changes)).toBe(after ?? "");
  });

  it("公式占位符作为整体比较，预览也不会从中截断", () => {
    const { changes } = diffText("由<m12/>得出结果。", "由<m13/>得出结果。");
    expect(changes.filter((c) => c.kind !== "equal")).toEqual([
      { kind: "removed", text: "<m12/>" },
      { kind: "added", text: "<m13/>" },
    ]);
    const preview = diffPreview(changes, 1, 1);
    expect(preview.find((c) => c.kind === "added")?.text).toBe("<m13/>");
  });

  it("共同长前缀后的修改会直接出现在折叠预览中", () => {
    const prefix = "相同的长段落前缀".repeat(2000);
    const result = diffText(`${prefix}旧方法结束。`, `${prefix}新方法结束。`);
    expect(result.coarse).toBe(false);
    const preview = diffPreview(result.changes);
    expect(preview.map((c) => c.text).join("")).toContain("旧新方法结束。");
    expect(preview[0]?.text.startsWith("…")).toBe(true);
    expect(preview.map((c) => c.text).join("").length).toBeLessThan(100);
  });

  it("完全改写的长段落按整块标记，限制比较矩阵开销且保留全文", () => {
    const before = "甲".repeat(1500);
    const after = "乙".repeat(1500);
    const result = diffText(before, after);
    expect(result.coarse).toBe(true);
    expect(result.changes).toEqual([
      { kind: "removed", text: before },
      { kind: "added", text: after },
    ]);
    expect(diffPreview(result.changes).every((c) => c.text.length <= 81)).toBe(true);
  });

  it("多处修改、重复文字和公式变化始终能还原完整前后译文", () => {
    const cases = [
      ["甲乙甲乙<m1/>丙丁", "甲丙甲乙<m2/>丁"],
      ["A method improves speed and quality.", "A robust method improves cost and quality."],
      ["原文\n第二行<m123/>", "译文\n第三行<m123/>！"],
    ];
    for (const [before, after] of cases) {
      const result = diffText(before!, after!);
      expect(original(result.changes)).toBe(before);
      expect(revised(result.changes)).toBe(after);
    }
  });
});
