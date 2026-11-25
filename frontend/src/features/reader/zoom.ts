import { readJSON, writeJSON } from "@/shared/storage/local";

export const SPINE = 2;
export const NARROW = 760;
export const WIDE_DEFAULT_PX = 1024;
export const COMFORT_MAX_SINGLE_PX = 900;
export const COMFORT_MAX_BOTH_PX = 820;
export const MIN_FIT_WIDTH_SCALE = 0.15;
export const MIN_MANUAL_ZOOM = 0.3;
export const MAX_MANUAL_ZOOM = 4;

export type ViewMode = "both" | "translated" | "original";
export type ReaderSlot = "narrow" | "wide";
export type ZoomSpec = { kind: "auto" } | { kind: "fit-width" } | { kind: "fixed"; value: number };

export interface ReaderPrefs {
  modeBySlot?: Partial<Record<ReaderSlot, ViewMode>>;
  zoom?: ZoomSpec;
}

const KEY = "pdftr.readerPrefs.v1";
export const AUTO_ZOOM: ZoomSpec = { kind: "auto" };

export function readerSlot(width: number): ReaderSlot {
  return width < WIDE_DEFAULT_PX ? "narrow" : "wide";
}

export function preferredMode(prefs: ReaderPrefs, slot: ReaderSlot): ViewMode {
  return prefs.modeBySlot?.[slot] ?? (slot === "wide" ? "both" : "translated");
}

function validMode(mode: unknown): mode is ViewMode {
  return mode === "both" || mode === "translated" || mode === "original";
}

export function readReaderPrefs(): ReaderPrefs {
  const stored = readJSON<ReaderPrefs | null>(KEY, null);
  const modeBySlot: ReaderPrefs["modeBySlot"] = {};
  for (const slot of ["narrow", "wide"] as const) {
    const mode = stored?.modeBySlot?.[slot];
    if (validMode(mode)) modeBySlot[slot] = mode;
  }
  const zoom = stored?.zoom;
  if (zoom?.kind === "auto" || zoom?.kind === "fit-width") return { modeBySlot, zoom: { kind: zoom.kind } };
  if (zoom?.kind === "fixed" && Number.isFinite(zoom.value) && zoom.value >= MIN_FIT_WIDTH_SCALE &&
      zoom.value <= MAX_MANUAL_ZOOM) return { modeBySlot, zoom: { kind: "fixed", value: zoom.value } };
  return { modeBySlot, zoom: AUTO_ZOOM };
}

export function writeReaderPrefs(patch: ReaderPrefs): ReaderPrefs {
  const current = readReaderPrefs();
  const next = { ...current, ...patch, modeBySlot: { ...current.modeBySlot, ...patch.modeBySlot } };
  writeJSON(KEY, next);
  return next;
}

export function computeScale({ contentWidth, columns, maxPageWidth, zoom }: {
  contentWidth: number;
  columns: 1 | 2;
  maxPageWidth: number;
  zoom: ZoomSpec;
}): { scale: number; autoScale: number; fitWidthScale: number } {
  const pageWidth = Math.max(1, maxPageWidth);
  const available = Math.max(120, contentWidth - (columns - 1) * SPINE);
  const fitWidthScale = Math.max(MIN_FIT_WIDTH_SCALE, available / columns / pageWidth);
  const comfort = columns === 1 ? COMFORT_MAX_SINGLE_PX : COMFORT_MAX_BOTH_PX;
  const autoScale = Math.min(fitWidthScale, comfort / pageWidth);
  const scale = zoom.kind === "auto" ? autoScale : zoom.kind === "fit-width" ? fitWidthScale
    : Math.min(MAX_MANUAL_ZOOM, Math.max(Math.min(MIN_MANUAL_ZOOM, fitWidthScale), zoom.value));
  return { scale, autoScale, fitWidthScale };
}
