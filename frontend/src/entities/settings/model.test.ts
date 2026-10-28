import { describe, expect, it } from "vitest";

import { DEFAULTS, authHeaders, getSettings, saveSettings } from "@/entities/settings/model";
import { readJSON } from "@/shared/storage/local";

describe("settings", () => {
  it("只发送填写了的请求头", () => {
    expect(authHeaders({ ...DEFAULTS })).toEqual({});
    expect(authHeaders({ ...DEFAULTS, llmKey: "k", model: "m" })).toEqual({ "X-LLM-Key": "k", "X-LLM-Model": "m" });
  });

  it("不记住 Key 时，Key 不写入浏览器存储但本页仍可用", () => {
    saveSettings({ ...DEFAULTS, llmKey: "secret", rememberKeys: false });
    expect(getSettings().llmKey).toBe("secret");
    expect(readJSON<Record<string, unknown>>("pdftr.settings.v1", {}).llmKey).toBe("");
    saveSettings({ ...DEFAULTS, llmKey: "secret", rememberKeys: true });
    expect(readJSON<Record<string, unknown>>("pdftr.settings.v1", {}).llmKey).toBe("secret");
  });
});
