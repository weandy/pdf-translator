import { expect, it } from "vitest";

import { allFilesChecked, confirmPendingDuplicates, removePendingDuplicates, uploadQueue } from "./batchDedup";

const fresh = { id: "fresh", state: "ready" as const, seen: null, confirmed: false, checking: false, pages: 3 };
const pending = { ...fresh, id: "pending", seen: true };
const confirmed = { ...pending, id: "confirmed", confirmed: true };
const checking = { ...pending, id: "checking", checking: true };
const limit = { ...pending, id: "limit", pages: 250 };
const errorFor = (item: typeof fresh | typeof pending) => item.pages > 200 ? "超限" : null;

it("批量确认与移除只作用于待确认的有效重复文件", () => {
  const items = [fresh, confirmed, pending, limit, checking];
  const selection = { items, pickSeq: 1, rangeText: "2" };
  expect(confirmPendingDuplicates(items, errorFor)).toEqual([fresh, confirmed, { ...pending, confirmed: true }, limit, checking]);
  expect(removePendingDuplicates(selection, errorFor).items).toEqual([fresh, confirmed, limit, checking]);
  expect(uploadQueue(items, errorFor)).toEqual([fresh, confirmed]);
  expect(allFilesChecked(items)).toBe(false);
});

it("移除后只剩一份 PDF 时清空旧页码", () => {
  expect(removePendingDuplicates({ items: [fresh, pending], pickSeq: 2, rangeText: "2" }, errorFor))
    .toEqual({ items: [fresh], pickSeq: 2, rangeText: "" });
});
