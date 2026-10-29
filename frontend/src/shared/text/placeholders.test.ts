import { describe, expect, it } from "vitest";

import { checkPlaceholders, placeholdersOk } from "@/shared/text/placeholders";

describe("checkPlaceholders", () => {
  const src = "Let <m1/> and <m2/> be given.";
  it("每个公式占位符各一次才通过", () => {
    expect(placeholdersOk(checkPlaceholders(src, "设 <m2/> 与 <m1/> 已知"))).toBe(true);
    expect(checkPlaceholders(src, "设 <m1/> 已知")).toEqual({ missing: ["m2"], repeated: [], unknown: [] });
    expect(checkPlaceholders(src, "<m1/><m1/><m2/>")).toEqual({ missing: [], repeated: ["m1"], unknown: [] });
    expect(checkPlaceholders(src, "<m1/><m2/><m10/>")).toEqual({ missing: [], repeated: [], unknown: ["m10"] });
  });
});
