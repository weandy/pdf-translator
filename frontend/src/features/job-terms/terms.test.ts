import { describe, expect, it } from "vitest";

import type { SegmentData, TermUse } from "@/shared/api/client";

import { renders, termRows } from "./terms";

const seg = (id: string, target: string, status = "done") => ({ id, target, status }) as SegmentData;
const use = (source: string, target: string, ids: string[], origin: TermUse["origin"] = "paper"): TermUse => ({
  source,
  target,
  origin,
  segment_ids: ids,
});

describe("本文术语", () => {
  it("译法有多个写法时任一即可，括号与空白不计", () => {
    expect(renders("注意力机制", "我们使用 注意力 机制。")).toBe(true);
    expect(renders("随机失活；丢弃法", "加入丢弃法以防过拟合")).toBe(true);
    expect(renders("残差网络（ResNet）", "残差网络的深度")).toBe(true);
    expect(renders("映射", "函数 f 把输入变成输出")).toBe(false);
  });

  it("我的术语库优先，按可能不一致的段数排序", () => {
    const segments = [seg("a", "用残差映射"), seg("b", "残差函数"), seg("c", "还没翻", "failed")];
    const rows = termRows(
      [use("residual", "残差", ["a", "b"]), use("mapping", "映射", ["a", "b", "c"])],
      [{ source: "Residual", target: "剩余", note: "", updatedAt: "" }],
      segments,
    );
    expect(rows.map((r) => [r.source, r.target, r.origin, r.changed, r.mismatchedIds])).toEqual([
      ["residual", "剩余", "user", true, ["a", "b"]], // 改成“剩余”后两段都没用上
      ["mapping", "映射", "paper", false, ["b"]], // 没翻好的段落不算
    ]);
  });
});
