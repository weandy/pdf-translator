import { afterEach, expect, it, vi } from "vitest";

const legacyKey = "pdftr.settings.v1";
const credsKey = "pdftr.credentials.v2";
const prefsKey = "pdftr.prefs.v1";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  vi.resetModules();
});

it.each([false, true])("按原绑定迁移旧 Key，未绑定必须确认（已绑定 %s）", async (bound) => {
  const host = "https://tenant.example.invalid/TeamA/v1";
  localStorage.setItem(legacyKey, JSON.stringify({
    protocol: "openai",
    baseUrl: host,
    llmKey: "synthetic-legacy",
    llmKeyBoundUrl: bound ? host : "",
    llmKeyBoundProtocol: bound ? "openai" : "",
    rememberKeys: true,
  }));
  const { authHeaders, getSettings, llmKeyNeedsRebind } = await import("./model");
  expect(getSettings().llmKey).toBe("synthetic-legacy");
  expect(llmKeyNeedsRebind(getSettings())).toBe(!bound);
  expect(authHeaders()["X-LLM-Key"]).toBe(bound ? "synthetic-legacy" : undefined);
  expect(JSON.parse(localStorage.getItem(credsKey)!).llmKeysByTarget)
    .toEqual(bound ? { ["openai|" + host]: "synthetic-legacy" } : {});
  expect(localStorage.getItem(prefsKey)).not.toContain("synthetic-legacy");
  expect(localStorage.getItem(legacyKey)).toBeNull();
});

it("存储事件尚未送达时也拒绝旧设置保存，普通偏好不复活已删除凭据", async () => {
  const vault = await import("./vault");
  expect(vault.saveSettings({ ...vault.DEFAULTS, llmKey: "synthetic-old", parserKey: "synthetic-mineru" }).ok).toBe(true);
  const oldDraft = vault.getSettings();
  const oldEpoch = vault.credentialEpoch;
  localStorage.removeItem(credsKey);
  expect(vault.savePrefs({ cloudConsent: true }).ok).toBe(true);
  expect(localStorage.getItem(credsKey)).toBeNull();
  expect(vault.saveSettings({ ...oldDraft, model: "updated" }, oldEpoch).ok).toBe(false);
  expect(vault.credentialEpoch).toBe(oldEpoch + 1);
  expect(vault.getSettings()).toMatchObject({ llmKey: "", parserKey: "", llmKeysByTarget: {} });
  expect(localStorage.getItem(credsKey)).toBeNull();
});
