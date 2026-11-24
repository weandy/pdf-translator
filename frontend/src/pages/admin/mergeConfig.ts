/**
 * 管理后台 409 乐观锁冲突时的按字段三方合并工具（baseline vs local vs remote）。
 * 仅远端改动的字段自动采用远端最新值，仅本地改动的字段保留，双方均改且不同时显式列明冲突。
 */
import type { SecretUpdate, SecretView } from "@/shared/api/admin";

export interface FieldConflict {
  label: string;
  localValue: string;
  remoteValue: string;
}

export interface MergeOutcome<T> {
  merged: T;
  adoptedFields: string[];
  keptLocalFields: string[];
  conflicts: FieldConflict[];
  dirty: boolean;
}

export interface MergeFieldSpec<T> {
  key: keyof T;
  label: string;
  equals?: (a: unknown, b: unknown) => boolean;
  format?: (value: unknown) => string;
}

function defaultFormat(value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (value === null || value === undefined || value === "") return "（空）";
  return String(value);
}

function applyField<T extends object, K extends keyof T>(
  merged: T,
  baseline: T,
  local: T,
  remote: T,
  key: K,
  label: string,
  eq: (a: unknown, b: unknown) => boolean,
  fmt: (value: unknown) => string,
  adoptedFields: string[],
  keptLocalFields: string[],
  conflicts: FieldConflict[],
): void {
  const baseVal = baseline[key];
  const localVal = local[key];
  const remoteVal = remote[key];

  const localChanged = !eq(localVal, baseVal);
  const remoteChanged = !eq(remoteVal, baseVal);

  if (!localChanged && remoteChanged) {
    merged[key] = remoteVal;
    adoptedFields.push(label);
  } else if (localChanged && !remoteChanged) {
    merged[key] = localVal;
    keptLocalFields.push(label);
  } else if (localChanged && remoteChanged && !eq(localVal, remoteVal)) {
    merged[key] = localVal;
    conflicts.push({
      label,
      localValue: fmt(localVal),
      remoteValue: fmt(remoteVal),
    });
  } else {
    merged[key] = localVal;
  }
}

/** 对扁平对象按字段执行三方合并。 */
export function mergeFields<T extends object>(
  baseline: T,
  local: T,
  remote: T,
  specs: readonly MergeFieldSpec<T>[],
): MergeOutcome<T> {
  const merged: T = { ...remote };
  const adoptedFields: string[] = [];
  const keptLocalFields: string[] = [];
  const conflicts: FieldConflict[] = [];

  for (const spec of specs) {
    const eq = spec.equals ?? ((a, b) => Object.is(a, b));
    const fmt = spec.format ?? defaultFormat;
    applyField(merged, baseline, local, remote, spec.key, spec.label, eq, fmt, adoptedFields, keptLocalFields, conflicts);
  }

  const dirty = specs.some((spec) => {
    const eq = spec.equals ?? ((a, b) => Object.is(a, b));
    return !eq(merged[spec.key], remote[spec.key]);
  });

  return { merged, adoptedFields, keptLocalFields, conflicts, dirty };
}

export function normalizeEndpointUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

export function secretViewEqual(a: SecretView, b: SecretView): boolean {
  return (
    a.mode === b.mode &&
    a.configured === b.configured &&
    a.mask === b.mask &&
    a.bound_url === b.bound_url &&
    a.bound_protocol === b.bound_protocol &&
    a.corrupted === b.corrupted
  );
}

export function formatSecretView(view: SecretView): string {
  if (view.corrupted) return "密文损坏";
  if (view.mode === "disabled") return "已禁用";
  if (view.mode === "custom" && view.configured) return `后台加密（${view.mask || "已配置"}）`;
  if (view.mode === "inherit_env" && view.configured) return `沿用 .env（${view.mask || "已配置"}）`;
  return "未配置";
}

export function formatSecretUpdate(update: SecretUpdate): string {
  switch (update.action) {
    case "replace":
      return "准备更换为新值";
    case "disable":
      return "准备清除并禁用";
    case "inherit_env":
      return "准备恢复 .env 默认";
    default:
      return "保持当前";
  }
}

/**
 * 合并一个加密凭据字段（本地是 SecretUpdate，基线与远端是 SecretView）。
 */
export function mergeSecretField(
  label: string,
  baseView: SecretView,
  remoteView: SecretView,
  localUpdate: SecretUpdate,
): {
  value: SecretUpdate;
  adopted?: string;
  kept?: string;
  conflict?: FieldConflict;
  dirty: boolean;
} {
  const localChanged = localUpdate.action !== "keep";
  const remoteChanged = !secretViewEqual(baseView, remoteView);

  if (!localChanged && remoteChanged) {
    return { value: { action: "keep", value: "" }, adopted: label, dirty: false };
  }
  if (localChanged && !remoteChanged) {
    return { value: localUpdate, kept: label, dirty: true };
  }
  if (localChanged && remoteChanged) {
    return {
      value: localUpdate,
      conflict: {
        label,
        localValue: formatSecretUpdate(localUpdate),
        remoteValue: formatSecretView(remoteView),
      },
      dirty: true,
    };
  }
  return { value: localUpdate, dirty: false };
}

/** 格式化三方合并完成后的提示文案。 */
export function formatMergeSummary(revision: number, outcome: Pick<MergeOutcome<unknown>, "adoptedFields" | "keptLocalFields" | "conflicts">): string {
  if (outcome.conflicts.length > 0) {
    return `已加载最新配置（v${revision}），但有 ${outcome.conflicts.length} 项同时在其他窗口被修改，已保留你的草稿，请核对后再保存或放弃草稿采用最新。`;
  }
  const parts: string[] = [];
  if (outcome.adoptedFields.length > 0) {
    parts.push(`已自动同步其他窗口对「${outcome.adoptedFields.join("、")}」的修改`);
  }
  if (outcome.keptLocalFields.length > 0) {
    parts.push(`已保留你对「${outcome.keptLocalFields.join("、")}」的本地修改`);
  }
  if (parts.length === 0) {
    return `已加载最新配置（v${revision}），当前草稿与最新配置一致。`;
  }
  return `已加载最新配置（v${revision}）并完成合并：${parts.join("，")}。确认无误后可再次点击保存。`;
}
