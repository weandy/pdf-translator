import type { ServerConfig, TranslationMode } from "@/shared/api/client";

import { effectiveNormalizedLlmTarget, normalizeEndpointUrl, normalizeLlmTarget } from "./target";
import { getSettings, saveSettings, type Settings } from "./vault";

export type { TranslationMode };
export { effectiveLlmTarget, effectiveNormalizedLlmTarget, type Protocol } from "./target";
export { clearKeys, DEFAULTS, getSettings, savePrefs, saveSettings, useSettings, type Settings } from "./vault";

export function isHttpUrl(url: string): boolean {
  return normalizeEndpointUrl(url) !== null;
}

export function llmKeyNeedsRebind(settings: Settings, config?: ServerConfig | null): boolean {
  if (!settings.llmKey.trim()) return false;
  const request = effectiveNormalizedLlmTarget(settings, config);
  const bound = normalizeLlmTarget(settings.llmKeyBoundProtocol, settings.llmKeyBoundUrl);
  return !request || !bound || request.key !== bound.key;
}

export function parserKeyNeedsRebind(settings: Settings, config?: ServerConfig | null): boolean {
  if (!settings.parserKey.trim()) return false;
  const request = normalizeEndpointUrl(config?.default_mineru_base_url ?? "");
  const bound = normalizeEndpointUrl(settings.parserKeyBoundUrl);
  return !request || request !== bound;
}

export function bindKeysForSave(
  settings: Settings,
  config?: ServerConfig | null,
  options?: { confirmCurrentTarget?: boolean },
): Settings {
  const request = effectiveNormalizedLlmTarget(settings, config);
  const bindLlm = request && (options?.confirmCurrentTarget || !llmKeyNeedsRebind(settings, config));
  const bindParser = options?.confirmCurrentTarget || !parserKeyNeedsRebind(settings, config);
  return {
    ...settings,
    llmKeyBoundUrl: settings.llmKey.trim() ? (bindLlm ? request.url : settings.llmKeyBoundUrl) : "",
    llmKeyBoundProtocol: settings.llmKey.trim() ? (bindLlm ? request.protocol : settings.llmKeyBoundProtocol) : "",
    parserKeyBoundUrl: settings.parserKey.trim()
      ? (bindParser ? (normalizeEndpointUrl(config?.default_mineru_base_url ?? "") ?? "") : settings.parserKeyBoundUrl)
      : "",
  };
}

export function confirmLlmKeyBinding(config: ServerConfig): ReturnType<typeof saveSettings> {
  const prev = getSettings();
  const target = effectiveNormalizedLlmTarget(prev, config);
  if (!target) return { ok: false, error: "翻译服务地址无效，请在设置中检查接口地址" };
  return saveSettings({ ...prev, llmKeyBoundUrl: target.url, llmKeyBoundProtocol: target.protocol });
}

export function confirmParserKeyBinding(settings: Settings, config: ServerConfig): Settings {
  return {
    ...settings,
    parserKeyBoundUrl: settings.parserKey.trim() ? (normalizeEndpointUrl(config.default_mineru_base_url ?? "") ?? "") : "",
  };
}

export function authHeaders(s: Settings = getSettings(), config?: ServerConfig | null): Record<string, string> {
  const headers: Record<string, string> = {};
  const key = s.llmKey.trim();
  const target = effectiveNormalizedLlmTarget(s, config);
  if (key && target) {
    headers["X-LLM-Protocol"] = target.protocol;
    headers["X-LLM-Base-URL"] = target.url;
    if (!llmKeyNeedsRebind(s, config)) headers["X-LLM-Key"] = key;
  } else if (!key) {
    if (s.protocol) headers["X-LLM-Protocol"] = s.protocol;
    const url = normalizeEndpointUrl(s.baseUrl);
    if (url) headers["X-LLM-Base-URL"] = url;
  }
  if (s.model.trim()) headers["X-LLM-Model"] = s.model.trim();
  const parser = normalizeEndpointUrl(s.parserKeyBoundUrl);
  if (s.parserKey.trim() && parser && (!config || !parserKeyNeedsRebind(s, config))) {
    headers["X-Parser-Key"] = s.parserKey.trim();
    headers["X-Parser-Base-URL"] = parser;
  }
  return headers;
}

export function resolveTranslationMode(settings: Settings, config?: ServerConfig | null): TranslationMode {
  if (!config?.free_channel?.enabled) return "byok";
  if (settings.translationMode) return settings.translationMode;
  return !settings.llmKey && !config.server_has_llm_key ? "free" : "byok";
}

export function uploadAuthHeaders(
  mode: TranslationMode,
  s: Settings = getSettings(),
  config?: ServerConfig | null,
): Record<string, string> {
  return mode === "free"
    ? { "X-Translation-Mode": "free" }
    : { ...authHeaders(s, config), "X-Translation-Mode": "byok" };
}
