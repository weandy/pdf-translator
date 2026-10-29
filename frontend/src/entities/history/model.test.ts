import { describe, expect, it } from "vitest";

import { addHistory, clearHistory, findByHash, HISTORY_MAX, removeHistory } from "@/entities/history/model";
import { fingerprint } from "@/shared/file/fingerprint";
import { readJSON } from "@/shared/storage/local";

const entry = (id: string, hash = "h1") => ({
  id,
  hash,
  filename: `${id}.pdf`,
  pages: 3,
  createdAt: "2026-10-29T10:00:00Z",
});

describe("本机翻译历史", () => {
  it("按指纹找到以前的翻译，最近的在前，有上限", () => {
    clearHistory();
    addHistory(entry("0000000000000001"));
    addHistory(entry("0000000000000002", "h2"));
    addHistory(entry("0000000000000003"));
    expect(findByHash("h1").map((e) => e.id)).toEqual(["0000000000000003", "0000000000000001"]);
    removeHistory("0000000000000003");
    expect(findByHash("h1").map((e) => e.id)).toEqual(["0000000000000001"]);
    for (let i = 0; i < HISTORY_MAX + 5; i++) addHistory(entry(String(i).padStart(16, "0"), "x"));
    expect(readJSON<unknown[]>("pdftr.history.v1", []).length).toBe(HISTORY_MAX);
  });

  it("同一内容的文件指纹相同", async () => {
    const a = await fingerprint(new Blob(["%PDF-1.4 same"]));
    const b = await fingerprint(new Blob(["%PDF-1.4 same"]));
    const c = await fingerprint(new Blob(["%PDF-1.4 other"]));
    expect(a).toMatch(/^[0-9a-f]{64}$/);
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });
});
