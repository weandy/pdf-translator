import { beforeEach, describe, expect, it } from "vitest";

import { initTheme, setLightPaperMode, setThemePreference, toggleDarkPaperMode, toggleTheme } from "./theme";

describe("主题切换", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
    delete document.documentElement.dataset.darkPaper;
    delete document.documentElement.dataset.lightPaper;
    document.head.querySelectorAll('meta[name="theme-color"]:not([media])').forEach((el) => el.remove());
    initTheme();
  });

  it("默认跟随系统，深色纸面为暗夜黑底、浅色纸面为原白纸", () => {
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(document.documentElement.dataset.darkPaper).toBe("night");
    expect(document.documentElement.dataset.lightPaper).toBe("white");
  });

  it("切换明暗主题时同步写入 data-theme 并持久化到本地", () => {
    const first = toggleTheme();
    expect(first).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(JSON.parse(window.localStorage.getItem("pdftr.theme.v1") ?? "null")).toBe("dark");

    const second = toggleTheme();
    expect(second).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");

    setThemePreference("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("支持深色纸面在暗夜黑底与柔暗原色之间切换并持久化", () => {
    expect(toggleDarkPaperMode()).toBe("dim");
    expect(document.documentElement.dataset.darkPaper).toBe("dim");
    expect(JSON.parse(window.localStorage.getItem("pdftr.dark_paper.v1") ?? "null")).toBe("dim");

    expect(toggleDarkPaperMode()).toBe("night");
    expect(document.documentElement.dataset.darkPaper).toBe("night");
  });

  it("支持浅色纸面在原白纸、柔灰纸与护眼绿之间切换并持久化", () => {
    setLightPaperMode("gray");
    expect(document.documentElement.dataset.lightPaper).toBe("gray");
    expect(JSON.parse(window.localStorage.getItem("pdftr.light_paper.v1") ?? "null")).toBe("gray");

    setLightPaperMode("green");
    expect(document.documentElement.dataset.lightPaper).toBe("green");
    expect(JSON.parse(window.localStorage.getItem("pdftr.light_paper.v1") ?? "null")).toBe("green");
  });

  it("将历史存储的 warm 浅色纸面平滑兼容映射为柔灰纸", () => {
    window.localStorage.setItem("pdftr.light_paper.v1", JSON.stringify("warm"));
    initTheme();
    expect(document.documentElement.dataset.lightPaper).toBe("gray");
  });
});
