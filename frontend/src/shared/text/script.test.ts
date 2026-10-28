import { describe, expect, it } from "vitest";

import { splitCaptionNo, splitScript } from "@/shared/text/script";

describe("splitScript", () => {
  it("中文与西文分开，中文引号归中文", () => {
    expect(splitScript("使用 Transformer 模型，“注意力”β1")).toEqual([
      { text: "使用", cjk: true },
      { text: " Transformer ", cjk: false },
      { text: "模型，“注意力”", cjk: true },
      { text: "β1", cjk: false },
    ]);
  });
});

describe("splitCaptionNo", () => {
  it("拆出图表编号", () => {
    expect(splitCaptionNo("图 4. 模型性能")).toEqual(["图 4.", " 模型性能"]);
    expect(splitCaptionNo("表2：结果")).toEqual(["表2：", "结果"]);
    expect(splitCaptionNo("如图 4 所示")).toEqual(["", "如图 4 所示"]);
  });
});
