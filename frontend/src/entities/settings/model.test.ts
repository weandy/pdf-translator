import { describe, expect, it } from "vitest";

import {
  DEFAULTS,
  authHeaders,
  bindKeysForSave,
  clearKeys,
  confirmLlmKeyBinding,
  confirmParserKeyBinding,
  effectiveLlmTarget,
  getSettings,
  isHttpUrl,
  llmKeyNeedsRebind,
  parserKeyNeedsRebind,
  resolveTranslationMode,
  saveSettings,
  uploadAuthHeaders,
} from "@/entities/settings/model";
import type { ServerConfig } from "@/shared/api/client";
import { CREDENTIALS_KEY, PREFS_KEY } from "./vault";

function makeConfig(overrides: Partial<ServerConfig> = {}): ServerConfig {
  return {
    version: "test",
    default_base_url: "https://example.invalid",
    default_mineru_base_url: "https://mineru.net",
    default_model: "synthetic-model",
    default_protocol: "openai",
    admin_path: "/weandy",
    server_has_llm_key: false,
    server_has_parser_token: true,
    server_has_paddle_token: false,
    max_upload_mb: 50,
    max_pages: 200,
    retention_hours: 24,
    submission_audit_enabled: false,
    attributions: [],
    paddle_public_enabled: false,
    maintenance_mode: false,
    site_notice: "",
    ...overrides,
  };
}

describe("settings", () => {
  it("按目标地址强绑定发送凭据头，安全停用未绑定个人凭据并钉死已绑定目标", () => {
    expect(authHeaders({ ...DEFAULTS })).toEqual({});

    // 未绑定目标地址的历史个人 LLM Key 与未绑定/旧占位符 "mineru-cloud" 的个人解析 Token 均被安全停用
    expect(
      authHeaders({
        ...DEFAULTS,
        llmKey: "sk-unbound",
        model: "m",
        parserKey: "tok-legacy",
        parserKeyBoundUrl: "mineru-cloud",
      }),
    ).toEqual({ "X-LLM-Model": "m" });
    expect(
      authHeaders({
        ...DEFAULTS,
        parserKey: "tok-empty",
        parserKeyBoundUrl: "",
      }),
    ).toEqual({});

    // 已绑定 llmKeyBoundUrl 且 baseUrl 为空时，必须显式输出 X-LLM-Base-URL 与 X-LLM-Protocol 防止站点改默认地址后误发
    expect(
      authHeaders({
        ...DEFAULTS,
        llmKey: "sk-bound",
        baseUrl: "",
        llmKeyBoundUrl: "https://bound.example.invalid/",
        llmKeyBoundProtocol: "anthropic",
        model: "claude-test",
        parserKey: "tok-bound",
        parserKeyBoundUrl: "https://mineru.example.invalid/",
      }, makeConfig({ default_base_url: "https://bound.example.invalid", default_protocol: "anthropic", default_mineru_base_url: "https://mineru.example.invalid" })),
    ).toEqual({
      "X-LLM-Protocol": "anthropic",
      "X-LLM-Base-URL": "https://bound.example.invalid",
      "X-LLM-Key": "sk-bound",
      "X-LLM-Model": "claude-test",
      "X-Parser-Key": "tok-bound",
      "X-Parser-Base-URL": "https://mineru.example.invalid",
    });

    // 无个人 LLM Key 时维持仅发用户显式填写的配置
    expect(
      authHeaders({
        ...DEFAULTS,
        protocol: "openai",
        baseUrl: "https://custom.example.invalid/",
        model: "m",
      }),
    ).toEqual({
      "X-LLM-Protocol": "openai",
      "X-LLM-Base-URL": "https://custom.example.invalid",
      "X-LLM-Model": "m",
    });
  });

  it("目标 B 搭配绑定到目标 A 的 Key 时只发目标信息", () => {
    const settings = { ...DEFAULTS, protocol: "openai" as const, baseUrl: "https://b.example.invalid/v1",
      llmKey: "synthetic-a", llmKeyBoundUrl: "https://a.example.invalid/v1", llmKeyBoundProtocol: "openai" as const };
    expect(llmKeyNeedsRebind(settings)).toBe(true);
    expect(authHeaders(settings)).toEqual({ "X-LLM-Protocol": "openai", "X-LLM-Base-URL": "https://b.example.invalid/v1" });
  });

  it("不记住 Key 时，Key 与绑定字段均不写入浏览器存储但本页仍可用；clearKeys 一并清空绑定字段", () => {
    saveSettings({
      ...DEFAULTS,
      llmKey: "secret",
      llmKeyBoundUrl: "https://example.invalid",
      llmKeyBoundProtocol: "openai",
      parserKey: "parser-secret",
      parserKeyBoundUrl: "mineru-cloud",
      rememberKeys: false,
    });
    expect(getSettings().llmKey).toBe("secret");
    expect(getSettings().llmKeyBoundUrl).toBe("https://example.invalid");
    expect(localStorage.getItem(CREDENTIALS_KEY)).toBeNull();
    expect(JSON.parse(localStorage.getItem(PREFS_KEY)!)).not.toHaveProperty("llmKey");

    saveSettings({
      ...DEFAULTS,
      llmKey: "secret",
      llmKeyBoundUrl: "https://example.invalid",
      llmKeyBoundProtocol: "openai",
      parserKey: "parser-secret",
      parserKeyBoundUrl: "mineru-cloud",
      rememberKeys: true,
    });
    expect(JSON.parse(localStorage.getItem(CREDENTIALS_KEY)!)).toMatchObject({ llmKey: "secret", llmKeyBoundUrl: "https://example.invalid" });

    clearKeys();
    expect(getSettings().llmKey).toBe("");
    expect(getSettings().llmKeyBoundUrl).toBe("");
    expect(getSettings().llmKeyBoundProtocol).toBe("");
    expect(getSettings().parserKey).toBe("");
    expect(getSettings().parserKeyBoundUrl).toBe("");
  });

  it("检测个人 Key 与站点默认地址/协议绑定状态，并在确认后写入绑定", () => {
    const cfg = makeConfig({ default_base_url: "https://api.deepseek.com", default_protocol: "openai" });

    expect(effectiveLlmTarget({ baseUrl: "", protocol: "" }, cfg)).toEqual({
      url: "https://api.deepseek.com",
      protocol: "openai",
    });

    // 老用户从未绑定过 llmKeyBoundUrl，依赖默认地址时必须重新确认
    const legacy = { ...DEFAULTS, llmKey: "sk-legacy", baseUrl: "" };
    expect(llmKeyNeedsRebind(legacy, cfg)).toBe(true);
    // 未确认前调用 bindKeysForSave 不会悄悄覆盖绑定绕过阻断
    expect(bindKeysForSave(legacy, cfg).llmKeyBoundUrl).toBe("");

    // 管理员修改默认地址或默认协议后必须重新确认
    const movedUrl = {
      ...DEFAULTS,
      llmKey: "sk-user",
      baseUrl: "",
      llmKeyBoundUrl: "https://old.example.invalid",
      llmKeyBoundProtocol: "openai" as const,
    };
    expect(llmKeyNeedsRebind(movedUrl, cfg)).toBe(true);
    const movedProto = {
      ...DEFAULTS,
      llmKey: "sk-user",
      baseUrl: "",
      llmKeyBoundUrl: "https://api.deepseek.com",
      llmKeyBoundProtocol: "anthropic" as const,
    };
    expect(llmKeyNeedsRebind(movedProto, cfg)).toBe(true);

    // 已绑定一致或填写了自定义 baseUrl 时不需要重绑
    expect(
      llmKeyNeedsRebind(
        {
          ...DEFAULTS,
          llmKey: "sk-user",
          baseUrl: "",
          llmKeyBoundUrl: "https://api.deepseek.com/",
          llmKeyBoundProtocol: "openai",
        },
        cfg,
      ),
    ).toBe(false);
    expect(llmKeyNeedsRebind({ ...DEFAULTS, llmKey: "sk-user", baseUrl: "https://custom.example.invalid" }, cfg)).toBe(
      true,
    );

    // 显式确认后写入绑定并解除阻断
    saveSettings(movedUrl);
    expect(confirmLlmKeyBinding(cfg).ok).toBe(true);
    const confirmed = getSettings();
    expect(confirmed.llmKeyBoundUrl).toBe("https://api.deepseek.com");
    expect(confirmed.llmKeyBoundProtocol).toBe("openai");
    expect(llmKeyNeedsRebind(getSettings(), cfg)).toBe(false);
  });

  it("检测个人 MinerU Token 与站点 default_mineru_base_url 的绑定状态并拦截历史占位值", () => {
    expect(isHttpUrl("https://mineru.net")).toBe(true);
    expect(isHttpUrl("http://127.0.0.1:8080")).toBe(true);
    expect(isHttpUrl("mineru-cloud")).toBe(false);
    expect(isHttpUrl("")).toBe(false);

    const cfg = makeConfig({ default_mineru_base_url: "https://mineru-alt.example.invalid/" });

    // 空绑定与历史 "mineru-cloud" 占位符均须重新确认
    const legacyEmpty = { ...DEFAULTS, parserKey: "tok-1", parserKeyBoundUrl: "" };
    const legacyPlaceholder = { ...DEFAULTS, parserKey: "tok-1", parserKeyBoundUrl: "mineru-cloud" };
    const oldHost = { ...DEFAULTS, parserKey: "tok-1", parserKeyBoundUrl: "https://mineru.net" };
    expect(parserKeyNeedsRebind(legacyEmpty, cfg)).toBe(true);
    expect(parserKeyNeedsRebind(legacyPlaceholder, cfg)).toBe(true);
    expect(parserKeyNeedsRebind(oldHost, cfg)).toBe(true);

    // 未确认时调用 bindKeysForSave 不会悄悄覆写绑定放行
    expect(bindKeysForSave(legacyPlaceholder, cfg).parserKeyBoundUrl).toBe("mineru-cloud");
    expect(bindKeysForSave(oldHost, cfg).parserKeyBoundUrl).toBe("https://mineru.net");

    // 显式确认后绑定规范化真实地址并解除阻断
    const confirmed = confirmParserKeyBinding(oldHost, cfg);
    expect(confirmed.parserKeyBoundUrl).toBe("https://mineru-alt.example.invalid");
    expect(parserKeyNeedsRebind(confirmed, cfg)).toBe(false);
    expect(bindKeysForSave(confirmed, cfg).parserKeyBoundUrl).toBe("https://mineru-alt.example.invalid");

    // 清空 parserKey 时清空绑定
    expect(bindKeysForSave({ ...confirmed, parserKey: "" }, cfg).parserKeyBoundUrl).toBe("");
  });

  it("按免费通道状态与用户配置解析默认翻译模式", () => {
    const freeOn = makeConfig({
      free_channel: { enabled: true, model_display: "demo-free", max_pages: 20, notice: "" },
    });
    const freeOff = makeConfig({
      free_channel: { enabled: false, model_display: "", max_pages: null, notice: "" },
    });

    // 免费通道未开启时始终走 byok
    expect(resolveTranslationMode({ ...DEFAULTS, translationMode: "free" }, null)).toBe("byok");
    expect(resolveTranslationMode({ ...DEFAULTS, translationMode: "free" }, freeOff)).toBe("byok");

    // 免费通道开启且未手动选择：无个人 Key 且无服务器 Key 默认走 free；已有个人 Key 或服务器 Key 保持 byok
    expect(resolveTranslationMode({ ...DEFAULTS }, freeOn)).toBe("free");
    expect(resolveTranslationMode({ ...DEFAULTS, llmKey: "sk-user" }, freeOn)).toBe("byok");
    expect(resolveTranslationMode({ ...DEFAULTS }, { ...freeOn, server_has_llm_key: true })).toBe("byok");

    // 免费通道开启且用户显式选择时尊重用户选择
    expect(resolveTranslationMode({ ...DEFAULTS, llmKey: "sk-user", translationMode: "free" }, freeOn)).toBe("free");
    expect(resolveTranslationMode({ ...DEFAULTS, translationMode: "byok" }, freeOn)).toBe("byok");
  });

  it("免费模式上传头不泄露任何 X-LLM-* 或 X-Parser-* 头，自备模式附带完整已绑定凭据与模式头", () => {
    const full = {
      ...DEFAULTS,
      protocol: "anthropic" as const,
      baseUrl: "https://custom.example.invalid",
      model: "claude-test",
      llmKey: "synthetic-personal",
      llmKeyBoundUrl: "https://custom.example.invalid",
      llmKeyBoundProtocol: "anthropic" as const,
      parserKey: "mineru-personal",
      parserKeyBoundUrl: "https://mineru.net",
    };

    const freeHeaders = uploadAuthHeaders("free", full);
    expect(freeHeaders).toEqual({
      "X-Translation-Mode": "free",
    });
    expect(Object.keys(freeHeaders).some((k) => k.startsWith("X-LLM-") || k.startsWith("X-Parser-"))).toBe(false);

    expect(uploadAuthHeaders("free", { ...full, parserKey: "" })).toEqual({
      "X-Translation-Mode": "free",
    });

    expect(uploadAuthHeaders("byok", full)).toEqual({
      "X-LLM-Protocol": "anthropic",
      "X-LLM-Base-URL": "https://custom.example.invalid",
      "X-LLM-Model": "claude-test",
      "X-LLM-Key": "synthetic-personal",
      "X-Parser-Key": "mineru-personal",
      "X-Parser-Base-URL": "https://mineru.net",
      "X-Translation-Mode": "byok",
    });
  });
});
