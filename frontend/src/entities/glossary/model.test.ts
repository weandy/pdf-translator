import { describe, expect, it } from "vitest";

import { findTerm, getTerms, removeTerm, termMap, toCsv, upsertTerms } from "@/entities/glossary/model";
import { readJSON } from "@/shared/storage/local";

describe("用户术语库", () => {
  it("按英文术语不分大小写覆盖，保存在浏览器里", () => {
    expect(upsertTerms([{ source: "Dropout", target: "随机失活", note: "" }])).toEqual({ added: 1, updated: 0 });
    expect(upsertTerms([{ source: " dropout ", target: "丢弃法", note: "组里约定" }])).toEqual({
      added: 0,
      updated: 1,
    });
    expect(findTerm("DROPOUT")?.target).toBe("丢弃法");
    expect(termMap()).toEqual({ dropout: "丢弃法" });
    expect(readJSON<unknown[]>("pdftr.glossary.v1", []).length).toBe(1);
    upsertTerms([{ source: "", target: "空", note: "" }]); // 空英文不保存
    expect(getTerms().length).toBe(1);
  });

  it("导出 CSV 带表头并转义逗号", () => {
    upsertTerms([{ source: "loss", target: "损失", note: "目标函数的值, 越小越好" }]);
    const csv = toCsv();
    expect(csv.startsWith("﻿英文,中文,说明\n")).toBe(true);
    expect(csv).toContain('loss,损失,"目标函数的值, 越小越好"');
    removeTerm("LOSS");
    expect(findTerm("loss")).toBeUndefined();
  });
});
