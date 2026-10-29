import { describe, expect, it } from "vitest";

import {
  findTerm,
  getTerms,
  planImport,
  removeTerm,
  removeTerms,
  restoreTerms,
  termMap,
  toCsv,
  toJson,
  toTsv,
  upsertTerms,
} from "@/entities/glossary/model";
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

  it("批量删除可以撤销，保留原来的修改时间", () => {
    upsertTerms(
      [
        { source: "encoder", target: "编码器", note: "" },
        { source: "decoder", target: "解码器", note: "" },
      ],
      "2026-01-01T00:00:00.000Z",
    );
    const removed = removeTerms(["Encoder", "decoder"]);
    expect(removed.map((t) => t.source)).toEqual(["encoder", "decoder"]);
    expect(findTerm("encoder")).toBeUndefined();
    restoreTerms(removed);
    expect(findTerm("decoder")?.updatedAt).toBe("2026-01-01T00:00:00.000Z");
    removeTerms(["encoder", "decoder"]);
  });

  it("导入前分成新词条、译法不同、完全相同", () => {
    upsertTerms([{ source: "attention", target: "注意力", note: "" }]);
    upsertTerms([{ source: "kernel", target: "卷积核", note: "" }]);
    const plan = planImport([
      { source: "Attention", target: "注意力", note: "" }, // 相同
      { source: "kernel", target: "核", note: "" }, // 译法不同
      { source: "stride", target: "步幅", note: "" }, // 新
      { source: "STRIDE", target: "步长", note: "" }, // 文件里重复，只算第一条
    ]);
    expect(plan.same).toBe(1);
    expect(plan.changed.map((c) => [c.old.target, c.incoming.target])).toEqual([["卷积核", "核"]]);
    expect(plan.fresh.map((t) => t.source)).toEqual(["stride"]);
    removeTerms(["attention", "kernel"]);
  });

  it("导出 TSV 与 JSON", () => {
    upsertTerms([{ source: "padding", target: "填充", note: "边缘\t补零" }]);
    const only = getTerms().filter((t) => t.source === "padding");
    expect(toTsv(only).split("\n")).toEqual(["英文\t中文\t说明", "padding\t填充\t边缘 补零"]);
    expect(JSON.parse(toJson(only))).toEqual([{ source: "padding", target: "填充", note: "边缘\t补零" }]);
    removeTerm("padding");
  });
});
