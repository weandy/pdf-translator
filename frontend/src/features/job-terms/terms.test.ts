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
    expect(renders("注意力机制", "我们使用 注意<b>力</b>机制。")).toBe(true);
    expect(renders("随机失活；丢弃法", "加入丢弃法以防过拟合")).toBe(true);
    expect(renders("残差网络（ResNet）", "残差网络的深度")).toBe(true);
    expect(renders("映射", "函数 f 把输入变成输出")).toBe(false);
  });

  it("我的术语库优先，按可能不一致的段数排序", () => {
    const segments = [seg("b", "残差函数"), seg("a", "用残差映射"), seg("c", "还没翻", "failed")];
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

  it("替代项拆分保留技术词的斜杠，并先剥去括号注释", () => {
    expect(renders("TCP/IP（传输协议/网际协议）；I/O", "采用 tcp/ip 或其他协议")).toBe(true);
    expect(renders("TCP/IP（传输协议/网际协议）；I/O", "仅用 i/o")).toBe(true);
    expect(renders("TCP/IP；I/O", "用 TCP、IP、I 和 O")).toBe(false);
    expect(renders("随机失活（A/B）/丢弃法", "用丢弃法")).toBe(true);
    expect(renders("随机失活（A/B）/丢弃法", "用 B")).toBe(false);
  });

  it("保留英文词界和词间空白，样式透明并由公式切断", () => {
    expect(renders("GAN", "organic")).toBe(false);
    expect(renders("RL", "world model 或 RL_model")).toBe(false);
    expect(renders("RL", "使用RL模型")).toBe(true);
    expect(renders("RL", "使用 <b>R</b>L 模型")).toBe(true);
    expect(renders("RL", "使用 R<m0/>L 模型")).toBe(false);
    expect(renders("注意力机制", "用注意力<m0/>机制")).toBe(false);
    expect(renders("graph network", "用 graph\t network")).toBe(true);
    expect(renders("graph network", "用 graphnetwork")).toBe(false);
    expect(renders("graph network", "用 graph <m1/> network")).toBe(false);
    expect(renders("graph <m1/> network", "用 graph <m2/> network")).toBe(false);
  });

  it("同段仅遮蔽被其他术语完整包住的具体命中", () => {
    const rows = termRows(
      [
        use("value", "数值", ["contained", "independent", "unrelated", "failed", "absent"]),
        use("estimated value", "估计数值", ["contained", "independent"]),
        use("right overlap", "数值范围", ["overlap"]),
        use("left overlap", "估计数值", ["overlap"]),
        use("same length", "估计数值", ["overlap"]),
      ],
      [],
      [
        seg("contained", "报告估计数值"),
        seg("independent", "报告估计数值及真值的数值", "edited"),
        seg("unrelated", "报告估计数值"),
        seg("overlap", "报告估计数值范围"),
        seg("failed", "", "failed"),
      ],
    );
    expect(Object.fromEntries(rows.map((r) => [r.source, r.mismatchedIds]))).toEqual({
      value: ["contained"],
      "estimated value": [],
      "right overlap": [],
      "left overlap": [],
      "same length": [],
    });
  });

  it("遮蔽使用当前用户术语译法，并接受独立替代项", () => {
    const rows = termRows(
      [
        use("policy gradients", "旧译", ["a", "b"]),
        use("filtered policy gradient", "过滤策略梯度", ["a", "b"]),
      ],
      [{ source: "policy-gradient", target: "策略梯度；PG", note: "", updatedAt: "" }],
      [seg("a", "计算过滤策略梯度"), seg("b", "计算过滤策略梯度，另报告PG")],
    );
    expect(rows[0]).toMatchObject({
      source: "policy gradients",
      target: "策略梯度；PG",
      origin: "user",
      changed: true,
      mismatchedIds: ["a"],
    });
  });
});
