/**
 * 用户设置：翻译服务与 Key。只保存在当前浏览器（docs/01-tech-stack.md §4.4.2）。
 * “不记住 Key”时，Key 只在本次打开的页面内存中有效。
 */
import { useSyncExternalStore } from "react";

import type { Quality, ServerConfig, TranslationMode } from "@/shared/api/client";
import { readJSON, remove, writeJSON } from "@/shared/storage/local";

export type { TranslationMode };
export type Protocol = "openai" | "anthropic";

export interface Settings {
  protocol: Protocol | ""; // 翻译服务的接口协议；空 = 用部署者的默认
  baseUrl: string;
  model: string;
  llmKey: string;
  llmKeyBoundUrl: string; // 个人翻译 Key 绑定的服务商地址，防止误发给其他地址
  llmKeyBoundProtocol: Protocol | ""; // 个人翻译 Key 绑定的协议
  parserKey: string; // MinerU Token
  parserKeyBoundUrl: string; // 个人解析 Token 绑定的服务标识
  quality: Quality;
  includeReferences: boolean;
  translationSide: "left" | "right"; // 双语对照时译文在左边还是右边
  rememberKeys: boolean;
  cloudConsent: boolean; // 已确认过“文件会上传到云端解析”：以后默认勾上（可以取消）
  translationMode: TranslationMode | ""; // 空串表示尚未手动选择，由 resolveTranslationMode 按配置决定
}

const KEY = "pdftr.settings.v1";

export const DEFAULTS: Settings = {
  protocol: "",
  baseUrl: "",
  model: "",
  llmKey: "",
  llmKeyBoundUrl: "",
  llmKeyBoundProtocol: "",
  parserKey: "",
  parserKeyBoundUrl: "",
  quality: "default",
  includeReferences: false,
  translationSide: "right",
  rememberKeys: true,
  cloudConsent: false,
  translationMode: "",
};

let current: Settings = { ...DEFAULTS, ...readJSON<Partial<Settings>>(KEY, {}) };
const listeners = new Set<() => void>();

export function getSettings(): Settings {
  return current;
}

export function saveSettings(next: Settings): void {
  current = next;
  const persisted = next.rememberKeys
    ? next
    : {
        ...next,
        llmKey: "",
        llmKeyBoundUrl: "",
        llmKeyBoundProtocol: "" as const,
        parserKey: "",
        parserKeyBoundUrl: "",
      };
  writeJSON(KEY, persisted);
  listeners.forEach((l) => l());
}

export function clearKeys(): void {
  saveSettings({
    ...current,
    llmKey: "",
    llmKeyBoundUrl: "",
    llmKeyBoundProtocol: "",
    parserKey: "",
    parserKeyBoundUrl: "",
  });
  if (!current.rememberKeys) remove(KEY);
}

export function normalizeBoundUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

export function isHttpUrl(url: string): boolean {
  return /^https?:\/\/[^/\s]+/i.test(url.trim());
}

/** 计算当前设置实际生效的翻译目标地址与协议。 */
export function effectiveLlmTarget(
  settings: Pick<Settings, "baseUrl" | "protocol">,
  config?: ServerConfig | null,
): { url: string; protocol: Protocol } {
  const url = settings.baseUrl.trim() || config?.default_base_url?.trim() || "";
  const protocol: Protocol = settings.protocol || config?.default_protocol || "openai";
  return { url, protocol };
}

/**
 * 判断浏览器里的个人翻译 Key 是否尚未绑定到当前站点默认地址/协议：
 * 仅在用户填了个人 Key 且依赖站点默认地址（baseUrl 为空）时触发，
 * 防止历史老用户或在管理员修改默认地址后把旧 Key 自动发往新地址。
 */
export function llmKeyNeedsRebind(settings: Settings, config?: ServerConfig | null): boolean {
  if (!settings.llmKey.trim()) return false;
  if (settings.baseUrl.trim()) return false;
  const defaultUrl = config?.default_base_url?.trim() ?? "";
  if (!defaultUrl) return false;
  if (!settings.llmKeyBoundUrl.trim()) return true;
  if (normalizeBoundUrl(settings.llmKeyBoundUrl) !== normalizeBoundUrl(defaultUrl)) return true;
  if (settings.llmKeyBoundProtocol && config?.default_protocol && settings.llmKeyBoundProtocol !== config.default_protocol) {
    return true;
  }
  return false;
}

/**
 * 判断浏览器里的个人 MinerU Token 是否尚未绑定到当前站点生效的 MinerU 基础地址：
 * 拦截未绑定空值、历史占位符（如 "mineru-cloud"）以及站点切换 MinerU 地址后的旧绑定。
 */
export function parserKeyNeedsRebind(settings: Settings, config?: ServerConfig | null): boolean {
  if (!settings.parserKey.trim()) return false;
  const defaultMineruUrl = config?.default_mineru_base_url?.trim() ?? "";
  if (!defaultMineruUrl) return false;
  if (!isHttpUrl(settings.parserKeyBoundUrl)) return true;
  return normalizeBoundUrl(settings.parserKeyBoundUrl) !== normalizeBoundUrl(defaultMineruUrl);
}

/**
 * 保存设置前补齐个人 Key 的目标绑定信息；
 * 若用户尚未确认默认地址变更（llmKeyNeedsRebind / parserKeyNeedsRebind 仍为 true 且未传入 confirmCurrentTarget），
 * 则保留原绑定值，避免在未确认的情况下隐式放行。
 */
export function bindKeysForSave(
  settings: Settings,
  config?: ServerConfig | null,
  options?: { confirmCurrentTarget?: boolean },
): Settings {
  const confirm = options?.confirmCurrentTarget ?? false;
  const hasLlmKey = Boolean(settings.llmKey.trim());
  const hasParserKey = Boolean(settings.parserKey.trim());
  let llmKeyBoundUrl = "";
  let llmKeyBoundProtocol: Protocol | "" = "";
  if (hasLlmKey) {
    if (!confirm && llmKeyNeedsRebind(settings, config)) {
      llmKeyBoundUrl = settings.llmKeyBoundUrl;
      llmKeyBoundProtocol = settings.llmKeyBoundProtocol;
    } else {
      const target = effectiveLlmTarget(settings, config);
      llmKeyBoundUrl = normalizeBoundUrl(target.url);
      llmKeyBoundProtocol = target.protocol;
    }
  }
  let parserKeyBoundUrl = "";
  if (hasParserKey) {
    const defaultMineruUrl = config?.default_mineru_base_url?.trim() ?? "";
    if (!confirm && parserKeyNeedsRebind(settings, config)) {
      parserKeyBoundUrl = settings.parserKeyBoundUrl;
    } else if (defaultMineruUrl && isHttpUrl(defaultMineruUrl)) {
      parserKeyBoundUrl = normalizeBoundUrl(defaultMineruUrl);
    } else if (isHttpUrl(settings.parserKeyBoundUrl)) {
      parserKeyBoundUrl = normalizeBoundUrl(settings.parserKeyBoundUrl);
    }
  }
  return {
    ...settings,
    llmKeyBoundUrl,
    llmKeyBoundProtocol,
    parserKeyBoundUrl,
  };
}

/** 用户显式确认把已有个人翻译 Key 发送到当前默认服务地址。 */
export function confirmLlmKeyBinding(config: ServerConfig): Settings {
  const prev = getSettings();
  const target = effectiveLlmTarget(prev, config);
  const next: Settings = {
    ...prev,
    llmKeyBoundUrl: normalizeBoundUrl(target.url),
    llmKeyBoundProtocol: target.protocol,
  };
  saveSettings(next);
  return next;
}

/** 将传入设置中的个人 MinerU Token 绑定到当前站点生效的 MinerU 基础地址。 */
export function confirmParserKeyBinding(settings: Settings, config: ServerConfig): Settings {
  if (!settings.parserKey.trim()) {
    return { ...settings, parserKeyBoundUrl: "" };
  }
  const defaultMineruUrl = config.default_mineru_base_url?.trim() ?? "";
  return {
    ...settings,
    parserKeyBoundUrl: isHttpUrl(defaultMineruUrl) ? normalizeBoundUrl(defaultMineruUrl) : "",
  };
}

export function useSettings(): Settings {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}

/**
 * 随请求发送的头：
 * - 填了个人 LLM Key 时，必须已绑定有效目标地址才输出 X-LLM-Key，并显式钉死 X-LLM-Base-URL 与 X-LLM-Protocol；
 *   未绑定目标地址的个人 Key 安全停用，防止默认地址变更后误发。
 * - 填了个人 MinerU Token 时，仅当绑定了合法 HTTP(S) URL 才输出 X-Parser-Key 与 X-Parser-Base-URL。
 */
export function authHeaders(s: Settings = current): Record<string, string> {
  const h: Record<string, string> = {};
  const llmKey = s.llmKey.trim();
  if (llmKey) {
    const targetUrl = normalizeBoundUrl(s.baseUrl || s.llmKeyBoundUrl);
    if (targetUrl) {
      h["X-LLM-Protocol"] = s.protocol || s.llmKeyBoundProtocol || "openai";
      h["X-LLM-Base-URL"] = targetUrl;
      h["X-LLM-Key"] = llmKey;
    } else {
      if (s.protocol) h["X-LLM-Protocol"] = s.protocol;
    }
  } else {
    if (s.protocol) h["X-LLM-Protocol"] = s.protocol;
    if (s.baseUrl.trim()) h["X-LLM-Base-URL"] = normalizeBoundUrl(s.baseUrl);
  }
  if (s.model.trim()) h["X-LLM-Model"] = s.model.trim();
  const parserKey = s.parserKey.trim();
  if (parserKey && isHttpUrl(s.parserKeyBoundUrl)) {
    h["X-Parser-Key"] = parserKey;
    h["X-Parser-Base-URL"] = normalizeBoundUrl(s.parserKeyBoundUrl);
  }
  return h;
}

/**
 * 解析当前生效的翻译通道模式：
 * - 免费通道未开启时始终走自备 API Key（byok）；
 * - 免费通道开启且用户已手动选择时，尊重用户偏好；
 * - 尚未手动选择时，无个人 Key 且无服务器默认 LLM Key 的新用户默认走免费体验，老用户默认保持 byok。
 */
export function resolveTranslationMode(settings: Settings, config?: ServerConfig | null): TranslationMode {
  if (!config?.free_channel?.enabled) return "byok";
  if (settings.translationMode === "free" || settings.translationMode === "byok") {
    return settings.translationMode;
  }
  return !settings.llmKey && !config.server_has_llm_key ? "free" : "byok";
}

/**
 * 构造上传任务的请求头：
 * - free 模式只附带 X-Translation-Mode: free，绝不发送任何个人翻译或解析凭据头；
 * - byok 模式透传完整 authHeaders 并附带 X-Translation-Mode: byok。
 */
export function uploadAuthHeaders(mode: TranslationMode, s: Settings = current): Record<string, string> {
  if (mode === "free") {
    return { "X-Translation-Mode": "free" };
  }
  return { ...authHeaders(s), "X-Translation-Mode": "byok" };
}
