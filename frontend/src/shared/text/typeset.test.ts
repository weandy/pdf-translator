import { describe, expect, it } from "vitest";

import type { BlockData, SegmentData } from "@/shared/api/client";
import { spaceCjkLatin, wantsIndent } from "@/shared/text/typeset";

// 用例与后端 tests/unit/test_typeset.py 一致
describe("spaceCjkLatin", () => {
  it("汉字与西文、数字、公式之间加空格，括号旁不加", () => {
    expect(spaceCjkLatin("使用Adam优化器，批量为128，准确率95%以上")).toBe("使用 Adam 优化器，批量为 128，准确率 95% 以上");
    expect(spaceCjkLatin("设<m1/>为函数")).toBe("设 <m1/> 为函数");
    expect(spaceCjkLatin("已有 空格 的Transformer（Adam）")).toBe("已有 空格 的 Transformer（Adam）");
  });
});

describe("wantsIndent", () => {
  const block = (type = "text") => ({ type, bold: false }) as BlockData;
  const seg = (source: string) => ({ source }) as SegmentData;
  it("正文段首缩进；跨栏后半段、公式后接着说、列表项、图注不缩进", () => {
    expect(wantsIndent(block(), seg("We propose a method."), true)).toBe(true);
    expect(wantsIndent(block(), seg("We propose a method."), false)).toBe(false);
    expect(wantsIndent(block(), seg("where x is the input."), true)).toBe(false);
    expect(wantsIndent(block(), seg("• First item"), true)).toBe(false);
    expect(wantsIndent(block(), seg("(1) First item"), true)).toBe(false);
    expect(wantsIndent(block("caption"), seg("Figure 1: A caption."), true)).toBe(false);
  });
});
