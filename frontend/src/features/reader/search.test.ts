import { describe, expect, it } from "vitest";

import { searchableText, searchDocument } from "./search";
import type { DocumentData } from "@/shared/api/client";

const doc = {
  title: "",
  pages: [{ index: 2, width: 600, height: 800, kind: "native", blocks: [{ id: "b1", page: 2, type: "text", bbox: { x0: 0, y0: 0, x1: 100, y1: 20 }, font_size: 10, bold: false, italic: false, serif: true, color: 0, translatable: true }] }],
  segments: [{ id: "s1", block_ids: ["b1"], parts: [{ block_id: "b1", chars: 10 }], source: "A <m1/> useful method", target: "一个有用的方法", status: "done", quality: "default", formulas: {} }],
  links: [],
  outline: [],
  job: {} as DocumentData["job"],
} as unknown as DocumentData;

describe("reader search", () => {
  it("removes formula placeholders and normalizes whitespace", () => {
    expect(searchableText(" A  <m1/> useful\nmethod ")).toBe("a useful method");
  });

  it("finds both source and translated text and keeps page location", () => {
    expect(searchDocument(doc, "有用")).toEqual([
      { segmentId: "s1", page: 2, source: "A <m1/> useful method", target: "一个有用的方法" },
    ]);
  });
});
