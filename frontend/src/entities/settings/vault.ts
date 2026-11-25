import { useSyncExternalStore } from "react";

import type { Quality, TranslationMode } from "@/shared/api/client";
import { removeChecked, writeJSONChecked, type StorageResult } from "@/shared/storage/local";

import { normalizeLlmTarget, type Protocol } from "./target";

export interface Prefs {
  protocol: Protocol | "";
  baseUrl: string;
  model: string;
  llmModelsByTarget: Record<string, string>;
  quality: Quality;
  includeReferences: boolean;
  translationSide: "left" | "right";
  rememberKeys: boolean;
  cloudConsent: boolean;
  translationMode: TranslationMode | "";
}

interface Credentials {
  llmKey: string;
  llmKeyBoundUrl: string;
  llmKeyBoundProtocol: Protocol | "";
  llmKeysByTarget: Record<string, string>;
  parserKey: string;
  parserKeyBoundUrl: string;
}

export type Settings = Prefs & Credentials;
export const PREFS_KEY = "pdftr.prefs.v1";
export const CREDENTIALS_KEY = "pdftr.credentials.v2";
const LEGACY_KEY = "pdftr.settings.v1";

const defaultPrefs: Prefs = {
  protocol: "",
  baseUrl: "",
  model: "",
  llmModelsByTarget: {},
  quality: "default",
  includeReferences: false,
  translationSide: "right",
  rememberKeys: true,
  cloudConsent: false,
  translationMode: "",
};

export const emptyCredentials: Credentials = {
  llmKey: "",
  llmKeyBoundUrl: "",
  llmKeyBoundProtocol: "",
  llmKeysByTarget: {},
  parserKey: "",
  parserKeyBoundUrl: "",
};

export const DEFAULTS: Settings = { ...defaultPrefs, ...emptyCredentials };

function pick<T extends object>(defaults: T, source?: Partial<T> | null): T {
  return Object.fromEntries(Object.entries(defaults).map(([key, fallback]) => {
    const value = source?.[key as keyof T];
    if (typeof fallback === "object") {
      const entries = value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [];
      return [key, Object.fromEntries(entries.filter(([name, entry]) =>
        (name.startsWith("openai|") || name.startsWith("anthropic|")) && typeof entry === "string"
      ))];
    }
    return [key, typeof value === typeof fallback ? value : fallback];
  })) as T;
}

function credentialsOf(settings?: Partial<Credentials> | null): Credentials {
  const credentials = pick(emptyCredentials, settings);
  const bound = normalizeLlmTarget(credentials.llmKeyBoundProtocol, credentials.llmKeyBoundUrl);
  if (bound && credentials.llmKey.trim()) {
    credentials.llmKeysByTarget = { ...credentials.llmKeysByTarget, [bound.key]: credentials.llmKey.trim() };
  }
  return credentials;
}

type PersistedCredentials = Credentials & { version: number; clearedAt: number };

function raw(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function parse<T>(value: string | null): Partial<T> | null {
  try {
    return value === null ? null : JSON.parse(value) as Partial<T> | null;
  } catch {
    return null;
  }
}

const initialPrefs = raw(PREFS_KEY);
let diskCredentials = raw(CREDENTIALS_KEY);
const legacyRaw = raw(LEGACY_KEY);
const legacy = initialPrefs === null ? parse<Settings>(legacyRaw) : null;
const startingPrefs = pick(defaultPrefs, parse<Prefs>(initialPrefs) ?? legacy);
let diskRemember = startingPrefs.rememberKeys;
let current: Settings = {
  ...startingPrefs,
  ...(diskRemember ? credentialsOf(parse<PersistedCredentials>(diskCredentials) ?? legacy) : emptyCredentials),
};
let migrationError: StorageResult | null = null;
if (legacy) {
  // 先写凭据，最后移除旧记录。中途失败后仍可重读原记录。
  if (startingPrefs.rememberKeys && diskCredentials === null) {
    migrationError = writeJSONChecked(CREDENTIALS_KEY, { ...credentialsOf(legacy), version: 1, clearedAt: 0 });
    diskCredentials = raw(CREDENTIALS_KEY);
  }
  if (migrationError?.ok !== false) migrationError = writeJSONChecked(PREFS_KEY, startingPrefs);
  if (migrationError?.ok !== false) migrationError = removeChecked(LEGACY_KEY);
} else if (initialPrefs !== null && legacyRaw !== null) {
  migrationError = removeChecked(LEGACY_KEY);
}

export let credentialEpoch = 0;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
function publish(next: Settings) {
  current = next;
  listeners.forEach((listener) => listener());
}

function syncCredentials(): boolean {
  const rawPrefs = raw(PREFS_KEY);
  const prefs = pick(defaultPrefs, parse<Prefs>(rawPrefs));
  const stored = raw(CREDENTIALS_KEY);
  const changed = stored !== diskCredentials || (diskRemember && !prefs.rememberKeys);
  diskCredentials = stored;
  diskRemember = prefs.rememberKeys;
  if (changed) {
    credentialEpoch += 1;
    publish({
      ...current,
      ...(rawPrefs === null ? {} : { rememberKeys: prefs.rememberKeys }),
      ...(prefs.rememberKeys ? credentialsOf(parse<PersistedCredentials>(stored)) : emptyCredentials),
    });
  }
  return changed;
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === null || event.key === CREDENTIALS_KEY || event.key === PREFS_KEY) syncCredentials();
  });
}

export function getSettings(): Settings {
  return current;
}

export function useSettings(): Settings {
  return useSyncExternalStore(subscribe, getSettings);
}

export function useCredentialEpoch(): number {
  return useSyncExternalStore(subscribe, () => credentialEpoch);
}

export function getMigrationError(): string | undefined {
  return migrationError?.ok === false ? migrationError.error : undefined;
}

export function savePrefs(patch: Partial<Omit<Prefs, "rememberKeys">>): StorageResult {
  const diskPrefs = pick(defaultPrefs, parse<Prefs>(raw(PREFS_KEY)) ?? current);
  const prefs = pick(defaultPrefs, { ...diskPrefs, ...patch, rememberKeys: diskPrefs.rememberKeys });
  const result = writeJSONChecked(PREFS_KEY, prefs);
  // 普通偏好可以在存储不可用时继续用于当前页面。
  publish({ ...current, ...pick(defaultPrefs, { ...current, ...patch, rememberKeys: current.rememberKeys }) });
  return result;
}

export function saveSettings(next: Settings, expectedEpoch = credentialEpoch): StorageResult {
  if (syncCredentials() || expectedEpoch !== credentialEpoch) {
    return { ok: false, error: "已保存的 Key 在其他标签页发生变化，请重新填写后保存" };
  }
  const prefs = pick(defaultPrefs, next);
  const credentials = credentialsOf(next);
  const changed = JSON.stringify(credentials) !== JSON.stringify(credentialsOf(current));
  if (changed || prefs.rememberKeys !== current.rememberKeys || migrationError?.ok === false) {
    let result: StorageResult;
    if (prefs.rememberKeys) {
      const previous = parse<PersistedCredentials>(diskCredentials);
      result = writeJSONChecked(CREDENTIALS_KEY, {
        ...credentials,
        version: (previous?.version ?? 0) + 1,
        clearedAt: previous?.clearedAt ?? 0,
      });
    } else {
      result = removeChecked(CREDENTIALS_KEY);
    }
    if (!result.ok) return result;
    diskCredentials = raw(CREDENTIALS_KEY);
    if (raw(LEGACY_KEY) !== null) {
      result = removeChecked(LEGACY_KEY);
      if (!result.ok) return result;
    }
  }
  const result = writeJSONChecked(PREFS_KEY, prefs);
  if (!result.ok) return result;
  diskRemember = prefs.rememberKeys;
  migrationError = null;
  publish({ ...prefs, ...credentials });
  return result;
}

export function clearKeys(expectedEpoch = credentialEpoch): StorageResult {
  if (syncCredentials() || expectedEpoch !== credentialEpoch) {
    return { ok: false, error: "已保存的 Key 在其他标签页发生变化，请重新确认后清除" };
  }
  const legacyResult = removeChecked(LEGACY_KEY);
  if (!legacyResult.ok) return legacyResult;
  const previous = parse<PersistedCredentials>(diskCredentials);
  const result = current.rememberKeys && previous
    ? writeJSONChecked(CREDENTIALS_KEY, {
        ...emptyCredentials,
        version: (previous.version ?? 0) + 1,
        clearedAt: Math.max(Date.now(), (previous.clearedAt ?? 0) + 1),
      })
    : removeChecked(CREDENTIALS_KEY);
  if (!result.ok) return result;
  diskCredentials = raw(CREDENTIALS_KEY);
  migrationError = null;
  credentialEpoch += 1;
  publish({ ...current, ...emptyCredentials });
  return result;
}
