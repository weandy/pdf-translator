import { useSyncExternalStore } from "react";

import { readJSON, writeJSON } from "@/shared/storage/local";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";
export type DarkPaperMode = "night" | "dim";
export type LightPaperMode = "white" | "gray" | "green";

const KEY = "pdftr.theme.v1";
const DARK_PAPER_KEY = "pdftr.dark_paper.v1";
const LIGHT_PAPER_KEY = "pdftr.light_paper.v1";
const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: "#eaeae3",
  dark: "#101113",
};

function isPreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

function isDarkPaperMode(value: unknown): value is DarkPaperMode {
  return value === "night" || value === "dim";
}

function isLightPaperMode(value: unknown): value is LightPaperMode {
  return value === "white" || value === "gray" || value === "green";
}

function readStoredPreference(): ThemePreference {
  const raw = readJSON<unknown>(KEY, "system");
  return isPreference(raw) ? raw : "system";
}

function readStoredDarkPaper(): DarkPaperMode {
  const raw = readJSON<unknown>(DARK_PAPER_KEY, "night");
  return isDarkPaperMode(raw) ? raw : "night";
}

function readStoredLightPaper(): LightPaperMode {
  const raw = readJSON<unknown>(LIGHT_PAPER_KEY, "white");
  if (raw === "warm") return "gray";
  return isLightPaperMode(raw) ? raw : "white";
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === "light" || preference === "dark") return preference;
  return systemPrefersDark() ? "dark" : "light";
}

let preference: ThemePreference = readStoredPreference();
let darkPaper: DarkPaperMode = readStoredDarkPaper();
let lightPaper: LightPaperMode = readStoredLightPaper();
let initialized = false;
const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((listener) => listener());
}

function applyTheme(next: ThemePreference): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (next === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = next;
  }
  root.dataset.darkPaper = darkPaper;
  root.dataset.lightPaper = lightPaper;
  const resolved = resolveTheme(next);
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]:not([media])');
  if (meta) {
    meta.content = THEME_COLORS[resolved];
  } else if (next !== "system") {
    const dynamic = document.createElement("meta");
    dynamic.name = "theme-color";
    dynamic.content = THEME_COLORS[resolved];
    document.head.appendChild(dynamic);
  }
}

export function initTheme(): void {
  preference = readStoredPreference();
  darkPaper = readStoredDarkPaper();
  lightPaper = readStoredLightPaper();
  applyTheme(preference);
  if (initialized || typeof window === "undefined" || typeof window.matchMedia !== "function") return;
  initialized = true;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (preference === "system") {
      applyTheme("system");
      notify();
    }
  };
  if (typeof media.addEventListener === "function") {
    media.addEventListener("change", onChange);
  }
}

export function setThemePreference(next: ThemePreference): void {
  preference = next;
  writeJSON(KEY, next);
  applyTheme(next);
  notify();
}

export function toggleTheme(): ResolvedTheme {
  const next: ResolvedTheme = resolveTheme(preference) === "dark" ? "light" : "dark";
  setThemePreference(next);
  return next;
}

export function setDarkPaperMode(next: DarkPaperMode): void {
  darkPaper = next;
  writeJSON(DARK_PAPER_KEY, next);
  if (typeof document !== "undefined") {
    document.documentElement.dataset.darkPaper = next;
  }
  notify();
}

export function toggleDarkPaperMode(): DarkPaperMode {
  const next: DarkPaperMode = darkPaper === "night" ? "dim" : "night";
  setDarkPaperMode(next);
  return next;
}

export function setLightPaperMode(next: LightPaperMode): void {
  lightPaper = next;
  writeJSON(LIGHT_PAPER_KEY, next);
  if (typeof document !== "undefined") {
    document.documentElement.dataset.lightPaper = next;
  }
  notify();
}

function getSnapshot(): string {
  return `${preference}:${resolveTheme(preference)}:${darkPaper}:${lightPaper}`;
}

export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  darkPaper: DarkPaperMode;
  lightPaper: LightPaperMode;
  toggle: () => void;
  setPreference: (next: ThemePreference) => void;
  setDarkPaper: (next: DarkPaperMode) => void;
  setLightPaper: (next: LightPaperMode) => void;
  toggleDarkPaper: () => void;
} {
  const snapshot = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot,
    () => "system:light:night:white",
  );
  const [pref, resolved, dark, light] = snapshot.split(":") as [
    ThemePreference,
    ResolvedTheme,
    DarkPaperMode,
    LightPaperMode,
  ];
  return {
    preference: pref,
    resolved,
    darkPaper: dark,
    lightPaper: light,
    toggle: toggleTheme,
    setPreference: setThemePreference,
    setDarkPaper: setDarkPaperMode,
    setLightPaper: setLightPaperMode,
    toggleDarkPaper: toggleDarkPaperMode,
  };
}
