import { afterEach, expect, it } from "vitest";

import { AUTO_ZOOM, computeScale, NARROW, preferredMode, readerSlot, readReaderPrefs, WIDE_DEFAULT_PX, writeReaderPrefs } from "./zoom";

afterEach(() => localStorage.clear());

it.each([390, 760, 900, 1024, 1440, 1920, 2560])("视口 %i 的净宽只扣一次内边距", (width) => {
  const contentWidth = width - (width <= NARROW ? 24 : 48);
  for (const columns of [1, 2] as const) {
    const fit = (contentWidth - (columns - 1) * 2) / columns / 600;
    const { scale, fitWidthScale } = computeScale({ contentWidth, columns, maxPageWidth: 600, zoom: AUTO_ZOOM });
    expect(fitWidthScale).toBeCloseTo(fit);
    expect(scale * 600).toBeCloseTo(Math.min(fit * 600, columns === 1 ? 900 : 820));
  }
});

it("极窄对照可缩至 15%，手动缩放保留真实适应宽度下限", () => {
  const geometry = { columns: 2 as const, contentWidth: 140, maxPageWidth: 600 };
  expect(computeScale({ ...geometry, zoom: { kind: "fit-width" } }).scale).toBe(0.15);
  expect(computeScale({ ...geometry, zoom: { kind: "fixed", value: 0.15 } }).scale).toBe(0.15);
});

it("视口偏好分槽独立于手机布局断点，初始化只读，丢弃非法偏好", () => {
  expect(NARROW).toBe(760);
  expect(WIDE_DEFAULT_PX).toBe(1024);
  expect(preferredMode(readReaderPrefs(), readerSlot(900))).toBe("translated");
  expect(preferredMode(readReaderPrefs(), readerSlot(1024))).toBe("both");
  expect(localStorage.getItem("pdftr.readerPrefs.v1")).toBeNull();
  writeReaderPrefs({ modeBySlot: { narrow: "original" } });
  writeReaderPrefs({ modeBySlot: { wide: "translated" }, zoom: { kind: "fixed", value: 1.25 } });
  expect(readReaderPrefs()).toEqual({ modeBySlot: { narrow: "original", wide: "translated" }, zoom: { kind: "fixed", value: 1.25 } });
  localStorage.setItem("pdftr.readerPrefs.v1", JSON.stringify({ modeBySlot: { narrow: "illegal", wide: "both" }, zoom: { kind: "fixed", value: -1 } }));
  expect(readReaderPrefs()).toEqual({ modeBySlot: { wide: "both" }, zoom: AUTO_ZOOM });
});
