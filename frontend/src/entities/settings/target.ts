import type { ServerConfig } from "@/shared/api/client";

export type Protocol = "openai" | "anthropic";

export function normalizeEndpointUrl(raw: string): string | null {
  if (/\p{Cc}/u.test(raw)) return null;
  const cleaned = raw.trim();
  if (/[\s?#\\]/.test(cleaned)) return null;
  const parts = /^(https?):\/\/([^/]+)(\/.*)?$/i.exec(cleaned);
  if (!parts || parts[2]!.includes("@")) return null;
  try {
    const parsed = new URL(cleaned);
    const host = parsed.hostname.replace(/\.+$/, "").toLowerCase();
    if (!host || parsed.port === "0") return null;
    const port = parsed.port ? ":" + parsed.port : "";
    // 保留原始路径，避免 URL 解析器折叠租户路径中的点段。
    const path = (parts[3] ?? "").replace(/\/+$/, "");
    return parsed.protocol + "//" + host + port + path;
  } catch {
    return null;
  }
}

export function normalizeLlmTarget(protocol: Protocol | "", rawUrl: string) {
  if (protocol !== "openai" && protocol !== "anthropic") return null;
  const url = normalizeEndpointUrl(rawUrl);
  return url ? { protocol, url, key: protocol + "|" + url } : null;
}

type TargetPrefs = { baseUrl: string; protocol: Protocol | "" };

export function effectiveLlmTarget(settings: TargetPrefs, config?: ServerConfig | null) {
  return {
    url: settings.baseUrl || config?.default_base_url || "",
    protocol: settings.protocol || config?.default_protocol || "openai",
  };
}

export function effectiveNormalizedLlmTarget(settings: TargetPrefs, config?: ServerConfig | null) {
  const { protocol, url } = effectiveLlmTarget(settings, config);
  return normalizeLlmTarget(protocol, url);
}
