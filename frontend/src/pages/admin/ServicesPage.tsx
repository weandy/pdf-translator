import { useCallback, useEffect, useRef, useState } from "react";

import { useAdminConsole } from "@/pages/admin/AdminLayout";
import {
  formatMergeSummary,
  formatSecretView,
  mergeFields,
  mergeSecretField,
  normalizeEndpointUrl,
  secretViewEqual,
  type FieldConflict,
  type MergeFieldSpec,
  type MergeOutcome,
} from "@/pages/admin/mergeConfig";
import {
  adminApi,
  AdminApiError,
  type AdminConfigView,
  type DefaultLlmConfig,
  type FreeProviderView,
  type GlossaryAdminView,
  type LlmPreset,
  type ModelPriceEntry,
  type ParserServiceView,
  type SecretUpdate,
  type SecretView,
} from "@/shared/api/admin";

type ServiceTab = "free" | "parser" | "pricing" | "glossary";
type ServiceSectionKey = "free" | "parser" | "llm" | "pricing" | "glossary";

interface FreeProviderDraft {
  id: string;
  name: string;
  enabled: boolean;
  protocol: "openai" | "anthropic";
  base_url: string;
  model: string;
  fallback_model: string;
  timeout_s: string;
  api_key: SecretUpdate;
  serverSecret: SecretView;
  originalUrl: string;
  originalProtocol: "openai" | "anthropic";
}

interface FreeDraft {
  enabled: boolean;
  max_pages: string;
  notice: string;
  providers: FreeProviderDraft[];
}

interface ParserDraft {
  mineru_base_url: string;
  mineru_token: SecretUpdate;
  mineru_proxy: SecretUpdate;
  paddle_public_enabled: boolean;
  paddle_token: SecretUpdate;
  paddle_proxy: SecretUpdate;
}

interface PresetDraft {
  id: string;
  name: string;
  protocol: "openai" | "anthropic";
  base_url: string;
  modelsText: string;
}

interface DefaultLlmDraft {
  base_url: string;
  protocol: "openai" | "anthropic";
  model: string;
  presets: PresetDraft[];
}

interface PriceRowDraft {
  model: string;
  cache_hit: string;
  cache_miss: string;
  output: string;
  peak_factor: string;
}

interface TestDiag {
  state: "idle" | "busy" | "ok" | "err";
  message: string;
}

interface SaveBanner {
  tone: "ok" | "err" | "warn" | "info";
  message: string;
  conflict?: boolean;
  conflicts?: FieldConflict[];
}

interface GlossaryDraft {
  order: string[];
  enabled: string[];
}

const KEEP_SECRET = (): SecretUpdate => ({ action: "keep", value: "" });

function parseStrictFinite(raw: string): number {
  const trimmed = raw.trim();
  if (!trimmed) return Number.NaN;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : Number.NaN;
}

const FREE_SCALAR_SPECS: readonly MergeFieldSpec<Pick<FreeDraft, "enabled" | "max_pages" | "notice">>[] = [
  { key: "enabled", label: "免费体验开关" },
  { key: "max_pages", label: "单篇页数上限" },
  { key: "notice", label: "前台提示文案" },
];

const FREE_PROVIDER_SCALAR_SPECS: readonly MergeFieldSpec<
  Pick<FreeProviderDraft, "name" | "enabled" | "protocol" | "base_url" | "model" | "fallback_model" | "timeout_s">
>[] = [
  { key: "name", label: "渠道名称" },
  { key: "enabled", label: "启用状态" },
  { key: "protocol", label: "接口协议" },
  {
    key: "base_url",
    label: "服务地址",
    equals: (a, b) => normalizeEndpointUrl(String(a ?? "")) === normalizeEndpointUrl(String(b ?? "")),
  },
  { key: "model", label: "主模型" },
  { key: "fallback_model", label: "备选模型" },
  { key: "timeout_s", label: "请求超时" },
];

const PARSER_SCALAR_SPECS: readonly MergeFieldSpec<Pick<ParserDraft, "mineru_base_url" | "paddle_public_enabled">>[] = [
  {
    key: "mineru_base_url",
    label: "MinerU 接口地址",
    equals: (a, b) => normalizeEndpointUrl(String(a ?? "")) === normalizeEndpointUrl(String(b ?? "")),
  },
  { key: "paddle_public_enabled", label: "PaddleOCR-VL 前台开放开关" },
];

const LLM_SCALAR_SPECS: readonly MergeFieldSpec<Pick<DefaultLlmDraft, "base_url" | "protocol" | "model">>[] = [
  { key: "base_url", label: "默认推荐地址" },
  { key: "protocol", label: "默认协议" },
  { key: "model", label: "默认模型" },
];

function providerEditableKey(p: FreeProviderDraft): string {
  return JSON.stringify({
    id: p.id,
    name: p.name,
    enabled: p.enabled,
    protocol: p.protocol,
    base_url: normalizeEndpointUrl(p.base_url),
    model: p.model,
    fallback_model: p.fallback_model,
    timeout_s: p.timeout_s,
    api_key: p.api_key,
  });
}

function providerRemoteKey(p: FreeProviderDraft): string {
  return JSON.stringify({
    id: p.id,
    name: p.name,
    enabled: p.enabled,
    protocol: p.protocol,
    base_url: normalizeEndpointUrl(p.base_url),
    model: p.model,
    fallback_model: p.fallback_model,
    timeout_s: p.timeout_s,
    secret: p.serverSecret,
  });
}

function editableFreeProvidersKey(providers: readonly FreeProviderDraft[]): string {
  return JSON.stringify(providers.map((p) => providerEditableKey(p)));
}

interface FreeMergeOutcome extends MergeOutcome<FreeDraft> {
  safetyBlockedIds: string[];
}

interface ParserMergeOutcome extends MergeOutcome<ParserDraft> {
  safetyBlocked: boolean;
}

function mergeFreeSection(base: FreeDraft, local: FreeDraft, remote: FreeDraft): FreeMergeOutcome {
  const scalar = mergeFields(
    { enabled: base.enabled, max_pages: base.max_pages, notice: base.notice },
    { enabled: local.enabled, max_pages: local.max_pages, notice: local.notice },
    { enabled: remote.enabled, max_pages: remote.max_pages, notice: remote.notice },
    FREE_SCALAR_SPECS,
  );
  const adoptedFields = [...scalar.adoptedFields];
  const keptLocalFields = [...scalar.keptLocalFields];
  const conflicts = [...scalar.conflicts];
  const safetyBlockedIds: string[] = [];

  const baseMap = new Map(base.providers.map((p) => [p.id, p]));
  const localMap = new Map(local.providers.map((p) => [p.id, p]));
  const remoteMap = new Map(remote.providers.map((p) => [p.id, p]));
  const allIds = [...new Set([...remoteMap.keys(), ...localMap.keys(), ...baseMap.keys()])];
  const mergedById = new Map<string, FreeProviderDraft>();

  for (const id of allIds) {
    const b = baseMap.get(id);
    const l = localMap.get(id);
    const r = remoteMap.get(id);

    if (b && l && r) {
      const pScalar = mergeFields(
        {
          name: b.name,
          enabled: b.enabled,
          protocol: b.protocol,
          base_url: b.base_url,
          model: b.model,
          fallback_model: b.fallback_model,
          timeout_s: b.timeout_s,
        },
        {
          name: l.name,
          enabled: l.enabled,
          protocol: l.protocol,
          base_url: l.base_url,
          model: l.model,
          fallback_model: l.fallback_model,
          timeout_s: l.timeout_s,
        },
        {
          name: r.name,
          enabled: r.enabled,
          protocol: r.protocol,
          base_url: r.base_url,
          model: r.model,
          fallback_model: r.fallback_model,
          timeout_s: r.timeout_s,
        },
        FREE_PROVIDER_SCALAR_SPECS,
      );
      const secretRes = mergeSecretField("API Key", b.serverSecret, r.serverSecret, l.api_key);
      const mergedP: FreeProviderDraft = {
        id: r.id,
        name: pScalar.merged.name,
        enabled: pScalar.merged.enabled,
        protocol: pScalar.merged.protocol,
        base_url: pScalar.merged.base_url,
        model: pScalar.merged.model,
        fallback_model: pScalar.merged.fallback_model,
        timeout_s: pScalar.merged.timeout_s,
        api_key: secretRes.value,
        serverSecret: r.serverSecret,
        originalUrl: r.base_url,
        originalProtocol: r.protocol,
      };
      const pLabel = mergedP.name || r.name || l.name || id;
      for (const field of pScalar.adoptedFields) adoptedFields.push(`上游「${pLabel}」· ${field}`);
      for (const field of pScalar.keptLocalFields) keptLocalFields.push(`上游「${pLabel}」· ${field}`);
      for (const c of pScalar.conflicts) conflicts.push({ ...c, label: `上游「${pLabel}」· ${c.label}` });

      const baseUrl = normalizeEndpointUrl(b.base_url);
      const localUrl = normalizeEndpointUrl(l.base_url);
      const remoteUrl = normalizeEndpointUrl(r.base_url);
      const mergedUrl = normalizeEndpointUrl(mergedP.base_url);
      const remoteEndpointChanged = remoteUrl !== baseUrl || r.protocol !== b.protocol;
      const localEndpointChanged = localUrl !== baseUrl || l.protocol !== b.protocol;
      const mergedEndpointDiffersFromLocal = mergedUrl !== localUrl || mergedP.protocol !== l.protocol;
      const remoteSecretChanged = !secretViewEqual(b.serverSecret, r.serverSecret);

      let pairingBlocked = false;
      if (
        (remoteEndpointChanged || mergedEndpointDiffersFromLocal) &&
        (l.api_key.action === "replace" || l.api_key.action === "inherit_env")
      ) {
        mergedP.api_key = KEEP_SECRET();
        pairingBlocked = true;
        safetyBlockedIds.push(id);
        conflicts.push({
          label: `上游「${pLabel}」· API Key（服务地址或协议已变更，已拦截自动配对，请重新输入新地址对应的密钥）`,
          localValue: `原目标 ${b.protocol} / ${b.base_url || "（空）"} 录入的新密钥（已清空）`,
          remoteValue: `新目标 ${mergedP.protocol} / ${mergedP.base_url || "（空）"}（当前密钥：${formatSecretView(r.serverSecret)}）`,
        });
      } else if (
        localEndpointChanged &&
        !remoteEndpointChanged &&
        remoteSecretChanged &&
        l.api_key.action === "keep"
      ) {
        mergedP.api_key = KEEP_SECRET();
        pairingBlocked = true;
        safetyBlockedIds.push(id);
        conflicts.push({
          label: `上游「${pLabel}」· API Key（本地已修改服务地址或协议且另一窗口更换了密钥，已拦截自动配对）`,
          localValue: `新目标 ${mergedP.protocol} / ${mergedP.base_url || "（空）"}（保持原密钥）`,
          remoteValue: `原目标 ${r.protocol} / ${r.base_url || "（空）"}（${formatSecretView(r.serverSecret)}）`,
        });
      }

      if (!pairingBlocked) {
        if (secretRes.adopted) adoptedFields.push(`上游「${pLabel}」· ${secretRes.adopted}`);
        if (secretRes.kept) keptLocalFields.push(`上游「${pLabel}」· ${secretRes.kept}`);
        if (secretRes.conflict) conflicts.push({ ...secretRes.conflict, label: `上游「${pLabel}」· ${secretRes.conflict.label}` });
      }
      mergedById.set(id, mergedP);
    } else if (!b && !l && r) {
      mergedById.set(id, r);
      adoptedFields.push(`新增上游「${r.name || id}」`);
    } else if (!b && l && !r) {
      mergedById.set(id, l);
      keptLocalFields.push(`新增上游「${l.name || id}」`);
    } else if (!b && l && r) {
      const sameEndpoint =
        normalizeEndpointUrl(l.base_url) === normalizeEndpointUrl(r.base_url) && l.protocol === r.protocol;
      const safeLocal: FreeProviderDraft = {
        ...l,
        serverSecret: r.serverSecret,
        originalUrl: r.base_url,
        originalProtocol: r.protocol,
        api_key: sameEndpoint ? l.api_key : KEEP_SECRET(),
      };
      if (!sameEndpoint && l.api_key.action === "replace") {
        safetyBlockedIds.push(id);
      }
      mergedById.set(id, safeLocal);
      conflicts.push({
        label: `上游「${l.name || r.name || id}」（双方同时新增同标识渠道）`,
        localValue: `${l.protocol} / ${l.base_url || "（空）"} / ${l.model || "（空）"}`,
        remoteValue: `${r.protocol} / ${r.base_url || "（空）"} / ${r.model || "（空）"}`,
      });
    } else if (b && !l && r) {
      if (providerRemoteKey(r) === providerRemoteKey(b)) {
        keptLocalFields.push(`删除上游「${b.name || id}」`);
      } else {
        mergedById.set(id, r);
        conflicts.push({
          label: `上游「${r.name || b.name || id}」`,
          localValue: "已删除该渠道",
          remoteValue: `已修改（模型 ${r.model || "空"}，地址 ${r.base_url || "空"}）`,
        });
      }
    } else if (b && l && !r) {
      if (providerEditableKey(l) === providerEditableKey(b)) {
        adoptedFields.push(`删除上游「${b.name || id}」`);
      } else {
        mergedById.set(id, l);
        conflicts.push({
          label: `上游「${l.name || b.name || id}」`,
          localValue: `已修改（模型 ${l.model || "空"}，地址 ${l.base_url || "空"}）`,
          remoteValue: "已在其他窗口删除",
        });
      }
    }
  }

  const baseCommon = base.providers.map((p) => p.id).filter((id) => mergedById.has(id));
  const localCommon = local.providers.map((p) => p.id).filter((id) => baseMap.has(id) && mergedById.has(id));
  const remoteCommon = remote.providers.map((p) => p.id).filter((id) => baseMap.has(id) && mergedById.has(id));
  const localReordered = JSON.stringify(localCommon) !== JSON.stringify(baseCommon);
  const remoteReordered = JSON.stringify(remoteCommon) !== JSON.stringify(baseCommon);

  const orderedIds: string[] = [];
  const pushUnique = (id: string) => {
    if (mergedById.has(id) && !orderedIds.includes(id)) orderedIds.push(id);
  };

  if (!localReordered && remoteReordered) {
    for (const p of remote.providers) pushUnique(p.id);
    for (const p of local.providers) pushUnique(p.id);
    adoptedFields.push("上游渠道优先级顺序");
  } else if (localReordered && !remoteReordered) {
    for (const p of local.providers) pushUnique(p.id);
    for (const p of remote.providers) pushUnique(p.id);
    keptLocalFields.push("上游渠道优先级顺序");
  } else if (localReordered && remoteReordered && JSON.stringify(localCommon) !== JSON.stringify(remoteCommon)) {
    for (const p of local.providers) pushUnique(p.id);
    for (const p of remote.providers) pushUnique(p.id);
    conflicts.push({
      label: "上游渠道优先级顺序",
      localValue: local.providers.map((p) => p.name || p.id).join(" > ") || "空",
      remoteValue: remote.providers.map((p) => p.name || p.id).join(" > ") || "空",
    });
  } else {
    for (const p of remote.providers) pushUnique(p.id);
    for (const p of local.providers) pushUnique(p.id);
  }
  for (const id of mergedById.keys()) pushUnique(id);

  const providers = orderedIds.map((id) => mergedById.get(id)!);
  const providersDirty = editableFreeProvidersKey(providers) !== editableFreeProvidersKey(remote.providers);
  return {
    merged: {
      enabled: scalar.merged.enabled,
      max_pages: scalar.merged.max_pages,
      notice: scalar.merged.notice,
      providers,
    },
    adoptedFields,
    keptLocalFields,
    conflicts,
    dirty: scalar.dirty || providersDirty || safetyBlockedIds.length > 0,
    safetyBlockedIds,
  };
}

function mergeParserSection(
  base: { draft: ParserDraft; view: ParserServiceView },
  local: ParserDraft,
  remoteCfg: AdminConfigView,
): ParserMergeOutcome {
  const remoteDraft = buildParserDraft(remoteCfg);
  const scalar = mergeFields(
    { mineru_base_url: base.draft.mineru_base_url, paddle_public_enabled: base.draft.paddle_public_enabled },
    { mineru_base_url: local.mineru_base_url, paddle_public_enabled: local.paddle_public_enabled },
    { mineru_base_url: remoteDraft.mineru_base_url, paddle_public_enabled: remoteDraft.paddle_public_enabled },
    PARSER_SCALAR_SPECS,
  );
  const adoptedFields = [...scalar.adoptedFields];
  const keptLocalFields = [...scalar.keptLocalFields];
  const conflicts = [...scalar.conflicts];
  let dirty = scalar.dirty;
  let safetyBlocked = false;

  const secretSpecs = [
    { key: "mineru_token", label: "MinerU API Token" },
    { key: "mineru_proxy", label: "MinerU 网络代理" },
    { key: "paddle_token", label: "PaddleOCR Token" },
    { key: "paddle_proxy", label: "PaddleOCR 网络代理" },
  ] as const;

  const merged: ParserDraft = {
    ...remoteDraft,
    mineru_base_url: scalar.merged.mineru_base_url,
    paddle_public_enabled: scalar.merged.paddle_public_enabled,
  };

  const baseUrl = normalizeEndpointUrl(base.draft.mineru_base_url);
  const localUrl = normalizeEndpointUrl(local.mineru_base_url);
  const remoteUrl = normalizeEndpointUrl(remoteDraft.mineru_base_url);
  const mergedUrl = normalizeEndpointUrl(merged.mineru_base_url);
  const mineruUrlChangedRemotely = remoteUrl !== baseUrl;
  const mineruUrlChangedLocally = localUrl !== baseUrl;
  const mergedUrlDiffersFromBase = mergedUrl !== baseUrl;
  const mergedUrlDiffersFromLocal = mergedUrl !== localUrl;

  for (const spec of secretSpecs) {
    if (spec.key === "mineru_token") {
      const localTok = local.mineru_token;
      const remoteTokView = remoteCfg.parser.mineru_token;
      const baseTokView = base.view.mineru_token;
      const remotePairedSafely =
        !mineruUrlChangedLocally &&
        mineruUrlChangedRemotely &&
        (!remoteTokView.configured || normalizeEndpointUrl(remoteTokView.bound_url) === mergedUrl);

      if (
        (localTok.action === "replace" || localTok.action === "inherit_env") &&
        (mineruUrlChangedRemotely || mergedUrlDiffersFromLocal)
      ) {
        merged.mineru_token = KEEP_SECRET();
        safetyBlocked = true;
        dirty = true;
        conflicts.push({
          label: "MinerU API Token（服务地址已变更，已拦截自动配对，请重新输入新地址对应的 Token）",
          localValue:
            localTok.action === "replace"
              ? `原地址 ${base.draft.mineru_base_url || "（空）"} 录入的新 Token（已清空）`
              : `原地址 ${base.draft.mineru_base_url || "（空）"} 选择恢复 .env 默认（已重置）`,
          remoteValue: `新地址 ${merged.mineru_base_url || "（空）"}（当前凭据：${formatSecretView(remoteTokView)}）`,
        });
        continue;
      }

      if (
        localTok.action === "keep" &&
        mergedUrlDiffersFromBase &&
        (baseTokView.configured || remoteTokView.configured) &&
        !remotePairedSafely
      ) {
        merged.mineru_token = KEEP_SECRET();
        safetyBlocked = true;
        dirty = true;
        conflicts.push({
          label: "MinerU API Token（服务地址已变更，已拦截原凭据自动配对，请重新输入新地址对应的 Token 或清除并禁用）",
          localValue: `目标地址 ${merged.mineru_base_url || "（空）"}（保持原凭据）`,
          remoteValue: `当前凭据 ${formatSecretView(remoteTokView)}（绑定地址：${remoteTokView.bound_url || base.draft.mineru_base_url || "未知"}）`,
        });
        continue;
      }
    }

    const res = mergeSecretField(spec.label, base.view[spec.key], remoteCfg.parser[spec.key], local[spec.key]);
    merged[spec.key] = res.value;
    if (res.adopted) adoptedFields.push(res.adopted);
    if (res.kept) keptLocalFields.push(res.kept);
    if (res.conflict) conflicts.push(res.conflict);
    if (res.dirty) dirty = true;
  }

  return { merged, adoptedFields, keptLocalFields, conflicts, dirty, safetyBlocked };
}

function mergeLlmSection(
  base: DefaultLlmDraft,
  local: DefaultLlmDraft,
  remote: DefaultLlmDraft,
): MergeOutcome<DefaultLlmDraft> {
  const scalar = mergeFields(
    { base_url: base.base_url, protocol: base.protocol, model: base.model },
    { base_url: local.base_url, protocol: local.protocol, model: local.model },
    { base_url: remote.base_url, protocol: remote.protocol, model: remote.model },
    LLM_SCALAR_SPECS,
  );
  const adoptedFields = [...scalar.adoptedFields];
  const keptLocalFields = [...scalar.keptLocalFields];
  const conflicts = [...scalar.conflicts];

  const basePresets = JSON.stringify(base.presets);
  const localPresets = JSON.stringify(local.presets);
  const remotePresets = JSON.stringify(remote.presets);
  const localChanged = localPresets !== basePresets;
  const remoteChanged = remotePresets !== basePresets;

  let presets: PresetDraft[];
  if (!localChanged && remoteChanged) {
    presets = remote.presets;
    adoptedFields.push("快捷服务商预设列表");
  } else if (localChanged && !remoteChanged) {
    presets = local.presets;
    keptLocalFields.push("快捷服务商预设列表");
  } else if (localChanged && remoteChanged && localPresets !== remotePresets) {
    presets = local.presets;
    conflicts.push({
      label: "快捷服务商预设列表",
      localValue: `${local.presets.length} 项（${local.presets.map((p) => p.name || p.id).join("、") || "空"}）`,
      remoteValue: `${remote.presets.length} 项（${remote.presets.map((p) => p.name || p.id).join("、") || "空"}）`,
    });
  } else {
    presets = local.presets;
  }

  return {
    merged: {
      base_url: scalar.merged.base_url,
      protocol: scalar.merged.protocol,
      model: scalar.merged.model,
      presets,
    },
    adoptedFields,
    keptLocalFields,
    conflicts,
    dirty: scalar.dirty || JSON.stringify(presets) !== remotePresets,
  };
}

function mergePricingSection(
  base: PriceRowDraft[],
  local: PriceRowDraft[],
  remote: PriceRowDraft[],
): MergeOutcome<PriceRowDraft[]> {
  const baseJson = JSON.stringify(base);
  const localJson = JSON.stringify(local);
  const remoteJson = JSON.stringify(remote);
  if (localJson === baseJson && remoteJson !== baseJson) {
    return { merged: remote, adoptedFields: ["模型单价表"], keptLocalFields: [], conflicts: [], dirty: false };
  }
  if (localJson !== baseJson && remoteJson === baseJson) {
    return { merged: local, adoptedFields: [], keptLocalFields: ["模型单价表"], conflicts: [], dirty: true };
  }
  if (localJson === remoteJson) {
    return { merged: remote, adoptedFields: [], keptLocalFields: [], conflicts: [], dirty: false };
  }

  // 尝试按模型名粒度合并不同模型的独立修改
  const uniqueNames = (rows: PriceRowDraft[]) => {
    const names = rows.map((r) => r.model.trim());
    return names.every(Boolean) && new Set(names).size === names.length;
  };
  if (uniqueNames(base) && uniqueNames(local) && uniqueNames(remote)) {
    const baseMap = new Map(base.map((r) => [r.model.trim(), r]));
    const localMap = new Map(local.map((r) => [r.model.trim(), r]));
    const remoteMap = new Map(remote.map((r) => [r.model.trim(), r]));
    const allNames = [...new Set([...remoteMap.keys(), ...localMap.keys(), ...baseMap.keys()])];
    const merged: PriceRowDraft[] = [];
    const adoptedFields: string[] = [];
    const keptLocalFields: string[] = [];
    const conflicts: FieldConflict[] = [];
    const rowEq = (a?: PriceRowDraft, b?: PriceRowDraft) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
    const fmtRow = (r?: PriceRowDraft) =>
      r ? `命中 ${r.cache_hit} / 未中 ${r.cache_miss} / 输出 ${r.output} / 倍率 ${r.peak_factor}` : "已删除";

    for (const name of allNames) {
      const bRow = baseMap.get(name);
      const lRow = localMap.get(name);
      const rRow = remoteMap.get(name);
      const lChanged = !rowEq(lRow, bRow);
      const rChanged = !rowEq(rRow, bRow);
      if (!lChanged && rChanged) {
        if (rRow) merged.push(rRow);
        adoptedFields.push(`模型「${name}」单价`);
      } else if (lChanged && !rChanged) {
        if (lRow) merged.push(lRow);
        keptLocalFields.push(`模型「${name}」单价`);
      } else if (lChanged && rChanged && !rowEq(lRow, rRow)) {
        if (lRow) merged.push(lRow);
        conflicts.push({
          label: `模型「${name}」单价`,
          localValue: fmtRow(lRow),
          remoteValue: fmtRow(rRow),
        });
      } else if (lRow) {
        merged.push(lRow);
      }
    }
    return {
      merged,
      adoptedFields,
      keptLocalFields,
      conflicts,
      dirty: JSON.stringify(merged) !== remoteJson,
    };
  }

  return {
    merged: local,
    adoptedFields: [],
    keptLocalFields: [],
    conflicts: [
      {
        label: "模型单价表",
        localValue: `${local.length} 个模型`,
        remoteValue: `${remote.length} 个模型（${remote.map((r) => r.model).join("、") || "空"}）`,
      },
    ],
    dirty: true,
  };
}

function mergeGlossarySection(
  base: GlossaryDraft,
  local: GlossaryDraft,
  remote: GlossaryDraft,
): MergeOutcome<GlossaryDraft> {
  const effective = (g: GlossaryDraft) => g.order.filter((id) => g.enabled.includes(id));
  const baseEff = JSON.stringify(effective(base));
  const localEff = JSON.stringify(effective(local));
  const remoteEff = JSON.stringify(effective(remote));
  const localChanged = localEff !== baseEff || JSON.stringify(local.order) !== JSON.stringify(base.order);
  const remoteChanged = remoteEff !== baseEff || JSON.stringify(remote.order) !== JSON.stringify(base.order);

  if (!localChanged && remoteChanged) {
    return {
      merged: remote,
      adoptedFields: ["术语库启用与优先级顺序"],
      keptLocalFields: [],
      conflicts: [],
      dirty: false,
    };
  }
  if (localChanged && !remoteChanged) {
    return {
      merged: local,
      adoptedFields: [],
      keptLocalFields: ["术语库启用与优先级顺序"],
      conflicts: [],
      dirty: true,
    };
  }
  if (localChanged && remoteChanged && localEff !== remoteEff) {
    return {
      merged: local,
      adoptedFields: [],
      keptLocalFields: [],
      conflicts: [
        {
          label: "术语库启用与优先级顺序",
          localValue: effective(local).join(" > ") || "无启用来源",
          remoteValue: effective(remote).join(" > ") || "无启用来源",
        },
      ],
      dirty: true,
    };
  }
  return {
    merged: remote,
    adoptedFields: [],
    keptLocalFields: [],
    conflicts: [],
    dirty: false,
  };
}

function buildFreeDraft(cfg: AdminConfigView): FreeDraft {
  return {
    enabled: cfg.free_translation.enabled,
    max_pages: cfg.free_translation.max_pages !== null ? String(cfg.free_translation.max_pages) : "",
    notice: cfg.free_translation.notice,
    providers: cfg.free_translation.providers.map((p: FreeProviderView) => ({
      id: p.id,
      name: p.name,
      enabled: p.enabled,
      protocol: p.protocol,
      base_url: p.base_url,
      model: p.model,
      fallback_model: p.fallback_model,
      timeout_s: String(p.timeout_s),
      api_key: KEEP_SECRET(),
      serverSecret: p.api_key,
      originalUrl: p.base_url,
      originalProtocol: p.protocol,
    })),
  };
}

function buildParserDraft(cfg: AdminConfigView): ParserDraft {
  return {
    mineru_base_url: cfg.parser.mineru_base_url,
    mineru_token: KEEP_SECRET(),
    mineru_proxy: KEEP_SECRET(),
    paddle_public_enabled: cfg.parser.paddle_public_enabled,
    paddle_token: KEEP_SECRET(),
    paddle_proxy: KEEP_SECRET(),
  };
}

function buildDefaultLlmDraft(llm: DefaultLlmConfig): DefaultLlmDraft {
  return {
    base_url: llm.base_url,
    protocol: llm.protocol,
    model: llm.model,
    presets: (llm.presets ?? []).map((item: LlmPreset) => ({
      id: item.id,
      name: item.name,
      protocol: item.protocol,
      base_url: item.base_url,
      modelsText: (item.models ?? []).join(", "),
    })),
  };
}

function buildPricingDraft(cfg: AdminConfigView): PriceRowDraft[] {
  return (cfg.pricing.models ?? []).map((item: ModelPriceEntry) => ({
    model: item.model,
    cache_hit: String(item.cache_hit),
    cache_miss: String(item.cache_miss),
    output: String(item.output),
    peak_factor: String(item.peak_factor),
  }));
}

function buildGlossaryOrder(glossary: GlossaryAdminView): string[] {
  const enabledOrder = glossary.sources.filter((id) => glossary.available.some((item) => item.id === id));
  const rest = glossary.available.map((item) => item.id).filter((id) => !enabledOrder.includes(id));
  return [...enabledOrder, ...rest];
}

function secretStatusBadge(view: SecretView): { text: string; tone: string } {
  if (view.corrupted) return { text: "密文损坏，需重填", tone: "err" };
  if (view.mode === "disabled") return { text: "已禁用", tone: "warn" };
  if (view.mode === "custom" && view.configured) return { text: "后台加密覆盖", tone: "ok" };
  if (view.mode === "inherit_env" && view.configured) return { text: "沿用 .env 默认", tone: "pen" };
  return { text: "未配置", tone: "" };
}

function SecretEditor({
  label,
  view,
  value,
  allowInheritEnv,
  placeholder,
  disabled,
  onChange,
}: {
  label: string;
  view: SecretView;
  value: SecretUpdate;
  allowInheritEnv: boolean;
  placeholder: string;
  disabled?: boolean;
  onChange: (next: SecretUpdate) => void;
}) {
  const badge = secretStatusBadge(view);
  return (
    <div className="ac-secret-box">
      <div className="ac-secret-row">
        <div className="ac-secret-mask">
          <span>{view.mask || (view.configured ? "已配置（加密）" : "未配置")}</span>
          <span className={`ac-badge ${badge.tone}`}>{badge.text}</span>
        </div>
        <button
          type="button"
          className={`ac-btn sm${value.action === "keep" ? " primary" : ""}`}
          disabled={disabled}
          onClick={() => onChange({ action: "keep", value: "" })}
        >
          保持当前
        </button>
        <button
          type="button"
          className={`ac-btn sm${value.action === "replace" ? " primary" : ""}`}
          disabled={disabled}
          onClick={() => onChange({ action: "replace", value: value.action === "replace" ? value.value : "" })}
        >
          更换{label}
        </button>
        {allowInheritEnv && (
          <button
            type="button"
            className={`ac-btn sm${value.action === "inherit_env" ? " primary" : ""}`}
            disabled={disabled}
            onClick={() => onChange({ action: "inherit_env", value: "" })}
          >
            恢复 .env 默认
          </button>
        )}
        <button
          type="button"
          className={`ac-btn sm danger`}
          aria-pressed={value.action === "disable"}
          disabled={disabled}
          onClick={() => onChange({ action: "disable", value: "" })}
        >
          清除并禁用
        </button>
      </div>
      {value.action === "replace" && (
        <input
          type="password"
          className="mono"
          autoComplete="new-password"
          placeholder={placeholder}
          value={value.value}
          disabled={disabled}
          onChange={(event) => onChange({ action: "replace", value: event.target.value })}
        />
      )}
      {value.action === "disable" && (
        <span className="ac-hint">保存后将清除已存储凭据并显式禁用（不会回退到环境变量）。</span>
      )}
      {value.action === "inherit_env" && (
        <span className="ac-hint">保存后将清除后台覆盖值，恢复沿用 .env 环境变量默认值。</span>
      )}
    </div>
  );
}

export function ServicesPage() {
  const { csrfToken, handleUnauthorized, syncSummary } = useAdminConsole();
  const [activeTab, setActiveTab] = useState<ServiceTab>("free");
  const [config, setConfig] = useState<AdminConfigView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  // 5 个分区各自独立的草稿、脏标记与安全阻断状态
  const [freeDraft, setFreeDraft] = useState<FreeDraft | null>(null);
  const [freeDirty, setFreeDirty] = useState(false);
  const [freeBanner, setFreeBanner] = useState<SaveBanner | null>(null);
  const [freeSaving, setFreeSaving] = useState(false);
  const [freeSafetyBlockedIds, setFreeSafetyBlockedIds] = useState<string[]>([]);

  const [parserDraft, setParserDraft] = useState<ParserDraft | null>(null);
  const [parserDirty, setParserDirty] = useState(false);
  const [parserBanner, setParserBanner] = useState<SaveBanner | null>(null);
  const [parserSaving, setParserSaving] = useState(false);
  const [parserSafetyBlocked, setParserSafetyBlocked] = useState(false);

  const [llmDraft, setLlmDraft] = useState<DefaultLlmDraft | null>(null);
  const [llmDirty, setLlmDirty] = useState(false);
  const [llmBanner, setLlmBanner] = useState<SaveBanner | null>(null);
  const [llmSaving, setLlmSaving] = useState(false);

  const [pricingDraft, setPricingDraft] = useState<PriceRowDraft[]>([]);
  const [pricingDirty, setPricingDirty] = useState(false);
  const [pricingBanner, setPricingBanner] = useState<SaveBanner | null>(null);
  const [pricingSaving, setPricingSaving] = useState(false);

  const [glossaryOrder, setGlossaryOrder] = useState<string[]>([]);
  const [glossaryEnabled, setGlossaryEnabled] = useState<string[]>([]);
  const [glossaryDirty, setGlossaryDirty] = useState(false);
  const [glossaryBanner, setGlossaryBanner] = useState<SaveBanner | null>(null);
  const [glossarySaving, setGlossarySaving] = useState(false);
  const [glossaryReloadingReq, setGlossaryReloadingReq] = useState(false);

  // 连接测试控制器与诊断状态：按 target/providerId 独立维护，修改表单或切换标签时自动中止过时请求
  const testControllersRef = useRef<Map<string, AbortController>>(new Map());
  const [testDiags, setTestDiags] = useState<Record<string, TestDiag>>({});
  const loadControllerRef = useRef<AbortController | null>(null);
  const dirtyRef = useRef({ free: false, parser: false, llm: false, pricing: false, glossary: false });
  const baselinesRef = useRef<{
    free: FreeDraft | null;
    parser: { draft: ParserDraft; view: ParserServiceView } | null;
    llm: DefaultLlmDraft | null;
    pricing: PriceRowDraft[] | null;
    glossary: GlossaryDraft | null;
  }>({ free: null, parser: null, llm: null, pricing: null, glossary: null });
  const draftsRef = useRef<{
    free: FreeDraft | null;
    parser: ParserDraft | null;
    llm: DefaultLlmDraft | null;
    pricing: PriceRowDraft[];
    glossary: GlossaryDraft;
  }>({ free: null, parser: null, llm: null, pricing: [], glossary: { order: [], enabled: [] } });

  useEffect(() => {
    dirtyRef.current = {
      free: freeDirty,
      parser: parserDirty,
      llm: llmDirty,
      pricing: pricingDirty,
      glossary: glossaryDirty,
    };
  }, [freeDirty, parserDirty, llmDirty, pricingDirty, glossaryDirty]);

  useEffect(() => {
    draftsRef.current = {
      free: freeDraft,
      parser: parserDraft,
      llm: llmDraft,
      pricing: pricingDraft,
      glossary: { order: glossaryOrder, enabled: glossaryEnabled },
    };
  }, [freeDraft, parserDraft, llmDraft, pricingDraft, glossaryOrder, glossaryEnabled]);

  const abortAllTests = useCallback(() => {
    for (const ctrl of testControllersRef.current.values()) {
      ctrl.abort();
    }
    testControllersRef.current.clear();
    setTestDiags((prev) => {
      const next: Record<string, TestDiag> = {};
      for (const [key, value] of Object.entries(prev)) {
        if (value.state !== "busy") next[key] = value;
      }
      return next;
    });
  }, []);

  const abortTestKey = useCallback((key: string) => {
    const existing = testControllersRef.current.get(key);
    if (existing) {
      existing.abort();
      testControllersRef.current.delete(key);
    }
    setTestDiags((prev) => {
      if (!prev[key]) return prev;
      const copy = { ...prev };
      delete copy[key];
      return copy;
    });
  }, []);

  const applyServerConfig = useCallback(
    (
      nextCfg: AdminConfigView,
      options?: {
        forceResetAll?: boolean;
        resetOnly?: ServiceSectionKey;
        mergeTrigger?: ServiceSectionKey;
      },
    ) => {
      const force = options?.forceResetAll ?? false;
      const only = options?.resetOnly;
      const trigger = options?.mergeTrigger;
      const dirtyFlags = { ...dirtyRef.current };

      const remoteFree = buildFreeDraft(nextCfg);
      const remoteParser = buildParserDraft(nextCfg);
      const remoteLlm = buildDefaultLlmDraft(nextCfg.default_llm);
      const remotePricing = buildPricingDraft(nextCfg);
      const remoteGlossary: GlossaryDraft = {
        order: buildGlossaryOrder(nextCfg.glossary),
        enabled: [...nextCfg.glossary.sources],
      };

      // 推进整页 revision 前统一处理全部 5 个分区：未脏的更新为远端，所有已脏分区一律三方合并并推进基线，绝不留过期草稿绕过下次 409。
      if (force || only === "free" || (!dirtyFlags.free && trigger !== "free")) {
        baselinesRef.current.free = remoteFree;
        draftsRef.current.free = remoteFree;
        dirtyRef.current.free = false;
        setFreeDraft(remoteFree);
        setFreeDirty(false);
        setFreeSafetyBlockedIds([]);
        if (force || only === "free") setFreeBanner(null);
      } else if (baselinesRef.current.free && draftsRef.current.free) {
        const outcome = mergeFreeSection(baselinesRef.current.free, draftsRef.current.free, remoteFree);
        baselinesRef.current.free = remoteFree;
        draftsRef.current.free = outcome.merged;
        dirtyRef.current.free = outcome.dirty;
        setFreeDraft(outcome.merged);
        setFreeDirty(outcome.dirty);
        setFreeSafetyBlockedIds(outcome.safetyBlockedIds);
        if (
          trigger === "free" ||
          outcome.adoptedFields.length > 0 ||
          outcome.conflicts.length > 0 ||
          outcome.safetyBlockedIds.length > 0
        ) {
          setFreeBanner({
            tone: outcome.conflicts.length > 0 || outcome.safetyBlockedIds.length > 0 ? "warn" : "info",
            message:
              outcome.safetyBlockedIds.length > 0
                ? `已加载最新配置（v${nextCfg.revision}），检测到上游地址或协议变更，已清空待保存旧密钥并拦截自动配对，请重新填写或采用最新配置。`
                : formatMergeSummary(nextCfg.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        }
      }

      if (force || only === "parser" || (!dirtyFlags.parser && trigger !== "parser")) {
        baselinesRef.current.parser = { draft: remoteParser, view: nextCfg.parser };
        draftsRef.current.parser = remoteParser;
        dirtyRef.current.parser = false;
        setParserDraft(remoteParser);
        setParserDirty(false);
        setParserSafetyBlocked(false);
        if (force || only === "parser") setParserBanner(null);
      } else if (baselinesRef.current.parser && draftsRef.current.parser) {
        const outcome = mergeParserSection(baselinesRef.current.parser, draftsRef.current.parser, nextCfg);
        baselinesRef.current.parser = { draft: remoteParser, view: nextCfg.parser };
        draftsRef.current.parser = outcome.merged;
        dirtyRef.current.parser = outcome.dirty;
        setParserDraft(outcome.merged);
        setParserDirty(outcome.dirty);
        setParserSafetyBlocked(outcome.safetyBlocked);
        if (
          trigger === "parser" ||
          outcome.adoptedFields.length > 0 ||
          outcome.conflicts.length > 0 ||
          outcome.safetyBlocked
        ) {
          setParserBanner({
            tone: outcome.conflicts.length > 0 || outcome.safetyBlocked ? "warn" : "info",
            message: outcome.safetyBlocked
              ? `已加载最新配置（v${nextCfg.revision}），检测到 MinerU 地址已变更，已清空待保存旧 Token 并拦截自动配对，请重新输入新地址对应的 Token 或采用最新配置。`
              : formatMergeSummary(nextCfg.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        }
      }

      if (force || only === "llm" || (!dirtyFlags.llm && trigger !== "llm")) {
        baselinesRef.current.llm = remoteLlm;
        draftsRef.current.llm = remoteLlm;
        dirtyRef.current.llm = false;
        setLlmDraft(remoteLlm);
        setLlmDirty(false);
        if (force || only === "llm") setLlmBanner(null);
      } else if (baselinesRef.current.llm && draftsRef.current.llm) {
        const outcome = mergeLlmSection(baselinesRef.current.llm, draftsRef.current.llm, remoteLlm);
        baselinesRef.current.llm = remoteLlm;
        draftsRef.current.llm = outcome.merged;
        dirtyRef.current.llm = outcome.dirty;
        setLlmDraft(outcome.merged);
        setLlmDirty(outcome.dirty);
        if (trigger === "llm" || outcome.adoptedFields.length > 0 || outcome.conflicts.length > 0) {
          setLlmBanner({
            tone: outcome.conflicts.length > 0 ? "warn" : "info",
            message: formatMergeSummary(nextCfg.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        }
      }

      if (force || only === "pricing" || (!dirtyFlags.pricing && trigger !== "pricing")) {
        baselinesRef.current.pricing = remotePricing;
        draftsRef.current.pricing = remotePricing;
        dirtyRef.current.pricing = false;
        setPricingDraft(remotePricing);
        setPricingDirty(false);
        if (force || only === "pricing") setPricingBanner(null);
      } else if (baselinesRef.current.pricing) {
        const outcome = mergePricingSection(baselinesRef.current.pricing, draftsRef.current.pricing, remotePricing);
        baselinesRef.current.pricing = remotePricing;
        draftsRef.current.pricing = outcome.merged;
        dirtyRef.current.pricing = outcome.dirty;
        setPricingDraft(outcome.merged);
        setPricingDirty(outcome.dirty);
        if (trigger === "pricing" || outcome.adoptedFields.length > 0 || outcome.conflicts.length > 0) {
          setPricingBanner({
            tone: outcome.conflicts.length > 0 ? "warn" : "info",
            message: formatMergeSummary(nextCfg.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        }
      }

      if (force || only === "glossary" || (!dirtyFlags.glossary && trigger !== "glossary")) {
        baselinesRef.current.glossary = remoteGlossary;
        draftsRef.current.glossary = remoteGlossary;
        dirtyRef.current.glossary = false;
        setGlossaryOrder(remoteGlossary.order);
        setGlossaryEnabled(remoteGlossary.enabled);
        setGlossaryDirty(false);
        if (force || only === "glossary") setGlossaryBanner(null);
      } else if (baselinesRef.current.glossary) {
        const outcome = mergeGlossarySection(baselinesRef.current.glossary, draftsRef.current.glossary, remoteGlossary);
        baselinesRef.current.glossary = remoteGlossary;
        draftsRef.current.glossary = outcome.merged;
        dirtyRef.current.glossary = outcome.dirty;
        setGlossaryOrder(outcome.merged.order);
        setGlossaryEnabled(outcome.merged.enabled);
        setGlossaryDirty(outcome.dirty);
        if (trigger === "glossary" || outcome.adoptedFields.length > 0 || outcome.conflicts.length > 0) {
          setGlossaryBanner({
            tone: outcome.conflicts.length > 0 ? "warn" : "info",
            message: formatMergeSummary(nextCfg.revision, outcome),
            conflicts: outcome.conflicts.length > 0 ? outcome.conflicts : undefined,
          });
        }
      }

      setConfig(nextCfg);
      syncSummary({
        revision: nextCfg.revision,
        secretKeyReady: nextCfg.secret_key_ready,
        maintenanceMode: nextCfg.system.maintenance_mode,
        warnings: nextCfg.warnings,
      });
    },
    [syncSummary],
  );

  const loadConfig = useCallback(
    async (options?: { forceResetAll?: boolean; resetOnly?: ServiceSectionKey; mergeTrigger?: ServiceSectionKey }) => {
      loadControllerRef.current?.abort();
      const controller = new AbortController();
      loadControllerRef.current = controller;
      setLoading(true);
      setLoadError("");
      try {
        const view = await adminApi.getConfig(controller.signal);
        if (controller.signal.aborted) return;
        applyServerConfig(view, options);
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        setLoadError(reason instanceof AdminApiError ? reason.message : "读取服务配置失败，请重试。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    },
    [applyServerConfig, handleUnauthorized],
  );

  useEffect(() => {
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    loadControllerRef.current = controller;
    void adminApi.getConfig(controller.signal).then(
      (view) => {
        if (controller.signal.aborted) return;
        setLoadError("");
        applyServerConfig(view, { forceResetAll: true });
        setLoading(false);
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        setLoadError(reason instanceof AdminApiError ? reason.message : "读取服务配置失败，请重试。");
        setLoading(false);
      },
    );
    return () => {
      controller.abort();
      abortAllTests();
    };
  }, [abortAllTests, applyServerConfig, handleUnauthorized]);

  function switchTab(next: ServiceTab) {
    if (next !== activeTab) {
      abortAllTests();
      setActiveTab(next);
    }
  }

  async function runConnectionTest(
    key: string,
    payload: Parameters<typeof adminApi.testConnection>[0],
  ) {
    abortTestKey(key);
    const controller = new AbortController();
    testControllersRef.current.set(key, controller);
    setTestDiags((prev) => ({
      ...prev,
      [key]: { state: "busy", message: "正在检测连接（禁重定向）…" },
    }));
    try {
      const result = await adminApi.testConnection(payload, csrfToken, controller.signal);
      if (controller.signal.aborted) return;
      testControllersRef.current.delete(key);
      if (result.ok) {
        const statusPart = result.status_code ? `HTTP ${result.status_code} · ` : "";
        setTestDiags((prev) => ({
          ...prev,
          [key]: {
            state: "ok",
            message: `测试通过：${statusPart}响应 ${result.latency_ms} ms${result.detail ? ` · ${result.detail}` : ""}`,
          },
        }));
      } else {
        setTestDiags((prev) => ({
          ...prev,
          [key]: {
            state: "err",
            message: `测试未通过：${result.detail || "上游未返回成功响应"}${result.status_code ? `（HTTP ${result.status_code}）` : ""}`,
          },
        }));
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      testControllersRef.current.delete(key);
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setTestDiags((prev) => ({
        ...prev,
        [key]: {
          state: "err",
          message: reason instanceof AdminApiError ? reason.message : "连接测试失败，请检查网络或配置。",
        },
      }));
    }
  }

  function formatSaveError(reason: unknown): SaveBanner {
    if (reason instanceof AdminApiError && reason.status === 409) {
      return {
        tone: "warn",
        message: "配置已在其他窗口更新，请加载最新并合并后再保存（当前表单草稿已保留）。",
        conflict: true,
      };
    }
    if (reason instanceof AdminApiError) {
      const fieldMsg = Object.values(reason.fields)[0];
      return { tone: "err", message: fieldMsg ?? reason.message };
    }
    return { tone: "err", message: "保存失败，请检查网络后重试。" };
  }

  // ---- 分区 1：免费翻译池操作 ----
  function updateProvider(index: number, patch: Partial<FreeProviderDraft>) {
    if (!freeDraft) return;
    const target = freeDraft.providers[index];
    if (!target) return;
    abortTestKey(`free:${target.id}`);
    if (patch.api_key !== undefined || patch.base_url !== undefined || patch.protocol !== undefined) {
      setFreeSafetyBlockedIds((prev) => (prev.includes(target.id) ? prev.filter((id) => id !== target.id) : prev));
    }
    setFreeDirty(true);
    setFreeBanner(null);
    setFreeDraft({
      ...freeDraft,
      providers: freeDraft.providers.map((item, idx) => (idx === index ? { ...item, ...patch } : item)),
    });
  }

  function addFreeProvider() {
    if (!freeDraft || freeDraft.providers.length >= 10) return;
    const existingIds = new Set(freeDraft.providers.map((item) => item.id));
    let seq = freeDraft.providers.length + 1;
    while (existingIds.has(`provider-${seq}`)) seq += 1;
    const newId = `provider-${seq}`;
    const emptySecret: SecretView = {
      mode: "custom",
      configured: false,
      mask: "",
      bound_url: "",
      bound_protocol: "",
      corrupted: false,
    };
    setFreeDirty(true);
    setFreeBanner(null);
    setFreeDraft({
      ...freeDraft,
      providers: [
        ...freeDraft.providers,
        {
          id: newId,
          name: `上游渠道 ${seq}`,
          enabled: true,
          protocol: "openai",
          base_url: "https://",
          model: "",
          fallback_model: "",
          timeout_s: "65",
          api_key: { action: "replace", value: "" },
          serverSecret: emptySecret,
          originalUrl: "",
          originalProtocol: "openai",
        },
      ],
    });
  }

  function moveFreeProvider(index: number, delta: -1 | 1) {
    if (!freeDraft) return;
    const nextIndex = index + delta;
    if (nextIndex < 0 || nextIndex >= freeDraft.providers.length) return;
    const nextList = [...freeDraft.providers];
    const [item] = nextList.splice(index, 1);
    if (!item) return;
    nextList.splice(nextIndex, 0, item);
    setFreeDirty(true);
    setFreeDraft({ ...freeDraft, providers: nextList });
  }

  function removeFreeProvider(index: number) {
    if (!freeDraft) return;
    const target = freeDraft.providers[index];
    if (target) {
      abortTestKey(`free:${target.id}`);
      setFreeSafetyBlockedIds((prev) => (prev.includes(target.id) ? prev.filter((id) => id !== target.id) : prev));
    }
    setFreeDirty(true);
    setFreeBanner(null);
    setFreeDraft({
      ...freeDraft,
      providers: freeDraft.providers.filter((_, idx) => idx !== index),
    });
  }

  async function saveFreeTranslation() {
    if (!config || !freeDraft || freeSaving || freeSafetyBlockedIds.length > 0) return;
    const parsedMaxPages = freeDraft.max_pages.trim() === "" ? null : Number(freeDraft.max_pages);
    if (parsedMaxPages !== null && (!Number.isInteger(parsedMaxPages) || parsedMaxPages < 1)) {
      setFreeBanner({ tone: "err", message: "单篇页数上限留空表示不限，填写时须为正整数。" });
      return;
    }
    setFreeSaving(true);
    setFreeBanner(null);
    try {
      const updated = await adminApi.updateConfig(
        "free_translation",
        {
          expected_revision: config.revision,
          enabled: freeDraft.enabled,
          max_pages: parsedMaxPages,
          notice: freeDraft.notice.trim(),
          providers: freeDraft.providers.map((p) => ({
            id: p.id.trim(),
            name: p.name.trim() || p.id.trim(),
            enabled: p.enabled,
            protocol: p.protocol,
            base_url: p.base_url.trim(),
            model: p.model.trim(),
            fallback_model: p.fallback_model.trim(),
            timeout_s: Math.max(15, Math.min(300, Number(p.timeout_s) || 65)),
            api_key: p.api_key,
          })),
        },
        csrfToken,
      );
      applyServerConfig(updated, { resetOnly: "free" });
      setFreeBanner({ tone: "ok", message: `免费翻译池已保存并热生效（v${updated.revision}）。` });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setFreeBanner(formatSaveError(reason));
    } finally {
      setFreeSaving(false);
    }
  }

  // ---- 分区 2：版面解析操作 ----
  function patchParser(patch: Partial<ParserDraft>, testTarget?: "mineru" | "paddle") {
    if (!parserDraft) return;
    if (testTarget) abortTestKey(`parser:${testTarget}`);
    if (patch.mineru_token !== undefined || patch.mineru_base_url !== undefined) {
      setParserSafetyBlocked(false);
    }
    setParserDirty(true);
    setParserBanner(null);
    setParserDraft({ ...parserDraft, ...patch });
  }

  async function saveParser() {
    if (!config || !parserDraft || parserSaving || parserSafetyBlocked) return;
    setParserSaving(true);
    setParserBanner(null);
    try {
      const updated = await adminApi.updateConfig(
        "parser",
        {
          expected_revision: config.revision,
          mineru_base_url: parserDraft.mineru_base_url.trim(),
          mineru_token: parserDraft.mineru_token,
          mineru_proxy: parserDraft.mineru_proxy,
          paddle_public_enabled: parserDraft.paddle_public_enabled,
          paddle_token: parserDraft.paddle_token,
          paddle_proxy: parserDraft.paddle_proxy,
        },
        csrfToken,
      );
      applyServerConfig(updated, { resetOnly: "parser" });
      setParserBanner({ tone: "ok", message: `版面解析设置已保存并热生效（v${updated.revision}）。` });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setParserBanner(formatSaveError(reason));
    } finally {
      setParserSaving(false);
    }
  }

  // ---- 分区 3A：自备默认推荐与快捷预设 ----
  async function saveDefaultLlm() {
    if (!config || !llmDraft || llmSaving) return;
    setLlmSaving(true);
    setLlmBanner(null);
    try {
      const updated = await adminApi.updateConfig(
        "default_llm",
        {
          expected_revision: config.revision,
          base_url: llmDraft.base_url.trim(),
          protocol: llmDraft.protocol,
          model: llmDraft.model.trim(),
          presets: llmDraft.presets.map((item) => ({
            id: item.id.trim(),
            name: item.name.trim(),
            protocol: item.protocol,
            base_url: item.base_url.trim(),
            models: item.modelsText
              .split(",")
              .map((m) => m.trim())
              .filter(Boolean),
          })),
        },
        csrfToken,
      );
      applyServerConfig(updated, { resetOnly: "llm" });
      setLlmBanner({ tone: "ok", message: `自备默认推荐与服务商预设已保存（v${updated.revision}）。` });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setLlmBanner(formatSaveError(reason));
    } finally {
      setLlmSaving(false);
    }
  }

  // ---- 分区 3B：模型单价热表 ----
  async function savePricing() {
    if (!config || pricingSaving) return;
    const models: ModelPriceEntry[] = [];
    for (const row of pricingDraft) {
      const name = row.model.trim();
      if (!name) continue;
      const cacheHit = parseStrictFinite(row.cache_hit);
      const cacheMiss = parseStrictFinite(row.cache_miss);
      const output = parseStrictFinite(row.output);
      const peakFactor = parseStrictFinite(row.peak_factor);
      if (
        !Number.isFinite(cacheHit) ||
        !Number.isFinite(cacheMiss) ||
        !Number.isFinite(output) ||
        !Number.isFinite(peakFactor) ||
        cacheHit < 0 ||
        cacheMiss < 0 ||
        output < 0 ||
        peakFactor <= 0 ||
        peakFactor < 1 ||
        peakFactor > 10
      ) {
        setPricingBanner({
          tone: "err",
          message: `模型「${name}」的单价或高峰倍率格式无效：单价须为非负有限数字，高峰倍率须为 1 至 10 之间的有限正数。`,
        });
        return;
      }
      models.push({
        model: name,
        cache_hit: cacheHit,
        cache_miss: cacheMiss,
        output,
        peak_factor: peakFactor,
      });
    }
    setPricingSaving(true);
    setPricingBanner(null);
    try {
      const updated = await adminApi.updateConfig(
        "pricing",
        {
          expected_revision: config.revision,
          models,
        },
        csrfToken,
      );
      applyServerConfig(updated, { resetOnly: "pricing" });
      setPricingBanner({ tone: "ok", message: `模型单价表已保存并实时生效（v${updated.revision}）。` });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setPricingBanner(formatSaveError(reason));
    } finally {
      setPricingSaving(false);
    }
  }

  // ---- 分区 4：公共术语库 ----
  function toggleGlossarySource(id: string) {
    setGlossaryDirty(true);
    setGlossaryBanner(null);
    setGlossaryEnabled((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  function moveGlossarySource(index: number, delta: -1 | 1) {
    const nextIndex = index + delta;
    if (nextIndex < 0 || nextIndex >= glossaryOrder.length) return;
    const nextOrder = [...glossaryOrder];
    const [moved] = nextOrder.splice(index, 1);
    if (!moved) return;
    nextOrder.splice(nextIndex, 0, moved);
    setGlossaryDirty(true);
    setGlossaryBanner(null);
    setGlossaryOrder(nextOrder);
  }

  async function saveGlossary() {
    if (!config || glossarySaving) return;
    const enabledInOrder = glossaryOrder.filter((id) => glossaryEnabled.includes(id));
    setGlossarySaving(true);
    setGlossaryBanner(null);
    try {
      const updated = await adminApi.updateConfig(
        "glossary",
        {
          expected_revision: config.revision,
          sources: enabledInOrder,
        },
        csrfToken,
      );
      applyServerConfig(updated, { resetOnly: "glossary" });
      setGlossaryBanner({ tone: "ok", message: `术语库配置已保存并触发后台异步重载（v${updated.revision}）。` });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setGlossaryBanner(formatSaveError(reason));
    } finally {
      setGlossarySaving(false);
    }
  }

  async function reloadGlossaryNow() {
    if (glossaryReloadingReq) return;
    setGlossaryReloadingReq(true);
    setGlossaryBanner(null);
    try {
      const view = await adminApi.reloadGlossary(csrfToken);
      setConfig((prev) => (prev ? { ...prev, glossary: view } : prev));
      setGlossaryBanner({
        tone: "ok",
        message: view.reloading
          ? "已触发后台异步重新加载术语库（期间沿用现有词库）。"
          : `术语库已就绪，当前已加载 ${view.loaded_terms.toLocaleString("zh-CN")} 词条。`,
      });
    } catch (reason) {
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setGlossaryBanner(formatSaveError(reason));
    } finally {
      setGlossaryReloadingReq(false);
    }
  }

  function adoptLatestSection(section: ServiceSectionKey) {
    if (!config) return;
    if (section === "free") {
      abortAllTests();
      const latest = buildFreeDraft(config);
      baselinesRef.current.free = latest;
      draftsRef.current.free = latest;
      dirtyRef.current.free = false;
      setFreeDraft(latest);
      setFreeDirty(false);
      setFreeSafetyBlockedIds([]);
      setFreeBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
    } else if (section === "parser") {
      abortAllTests();
      const latest = buildParserDraft(config);
      baselinesRef.current.parser = { draft: latest, view: config.parser };
      draftsRef.current.parser = latest;
      dirtyRef.current.parser = false;
      setParserDraft(latest);
      setParserDirty(false);
      setParserSafetyBlocked(false);
      setParserBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
    } else if (section === "llm") {
      const latest = buildDefaultLlmDraft(config.default_llm);
      baselinesRef.current.llm = latest;
      draftsRef.current.llm = latest;
      dirtyRef.current.llm = false;
      setLlmDraft(latest);
      setLlmDirty(false);
      setLlmBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
    } else if (section === "pricing") {
      const latest = buildPricingDraft(config);
      baselinesRef.current.pricing = latest;
      draftsRef.current.pricing = latest;
      dirtyRef.current.pricing = false;
      setPricingDraft(latest);
      setPricingDirty(false);
      setPricingBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
    } else {
      const latest: GlossaryDraft = {
        order: buildGlossaryOrder(config.glossary),
        enabled: [...config.glossary.sources],
      };
      baselinesRef.current.glossary = latest;
      draftsRef.current.glossary = latest;
      dirtyRef.current.glossary = false;
      setGlossaryOrder(latest.order);
      setGlossaryEnabled(latest.enabled);
      setGlossaryDirty(false);
      setGlossaryBanner({ tone: "info", message: `已采用最新配置（v${config.revision}）覆盖本地草稿。` });
    }
  }

  function renderBanner(section: ServiceSectionKey, banner: SaveBanner | null) {
    if (!banner) return null;
    return (
      <div
        className={`ac-alert ${banner.tone}`}
        role={banner.tone === "ok" || banner.tone === "info" ? "status" : "alert"}
      >
        <div className="ac-alert-body">
          <span>{banner.message}</span>
          {banner.conflicts && banner.conflicts.length > 0 && (
            <ul className="ac-conflict-list">
              {banner.conflicts.map((item) => (
                <li key={item.label}>
                  <strong>{item.label}</strong>：你的草稿「{item.localValue}」 / 最新配置「{item.remoteValue}」
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="ac-actions">
          {banner.conflict && (
            <button type="button" className="ac-btn sm" onClick={() => void loadConfig({ mergeTrigger: section })}>
              加载最新并合并
            </button>
          )}
          {(banner.conflict || (banner.conflicts && banner.conflicts.length > 0)) && (
            <button
              type="button"
              className="ac-btn sm"
              onClick={() =>
                banner.conflict ? void loadConfig({ resetOnly: section }) : adoptLatestSection(section)
              }
            >
              全部采用最新覆盖草稿
            </button>
          )}
        </div>
      </div>
    );
  }

  if (loading && !config) {
    return (
      <div className="ac-card" role="status">
        正在读取服务配置…
      </div>
    );
  }

  if (loadError && !config) {
    return (
      <div className="ac-alert err" role="alert">
        <span>{loadError}</span>
        <button type="button" className="ac-btn sm" onClick={() => void loadConfig({ forceResetAll: true })}>
          重试
        </button>
      </div>
    );
  }

  if (!config || !freeDraft || !parserDraft || !llmDraft) return null;

  const sourceMap = new Map(config.glossary.available.map((item) => [item.id, item]));
  const mineruDiag = testDiags["parser:mineru"];
  const paddleDiag = testDiags["parser:paddle"];

  return (
    <>
      <div className="ac-subtabs" role="tablist" aria-label="服务设置分区">
        {(
          [
            { key: "free", label: "免费翻译池" },
            { key: "parser", label: "版面解析" },
            { key: "pricing", label: "自备预设与计价" },
            { key: "glossary", label: "公共术语库" },
          ] as const
        ).map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            className={`ac-subtab${activeTab === tab.key ? " active" : ""}`}
            onClick={() => switchTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* 分区 1：免费翻译池 */}
      {activeTab === "free" && (
        <section className="ac-card" aria-labelledby="svc-free-heading">
          <div className="ac-card-hd">
            <div>
              <h2 className="ac-card-title" id="svc-free-heading">
                免费翻译多上游渠道池
              </h2>
              <p className="ac-card-sub">按优先级自动调度与跨服务商容灾；请求参数由服务端自动适配。</p>
            </div>
            <label className="ac-toggle">
              <input
                type="checkbox"
                checked={freeDraft.enabled}
                onChange={(event) => {
                  setFreeDirty(true);
                  setFreeBanner(null);
                  setFreeDraft({ ...freeDraft, enabled: event.target.checked });
                }}
              />
              <span>{freeDraft.enabled ? "已开启免费体验" : "已关闭免费体验"}</span>
            </label>
          </div>

          {renderBanner("free", freeBanner)}

          <div className="ac-grid-2" style={{ marginTop: freeBanner ? 12 : 0, marginBottom: 14 }}>
            <div className="ac-field">
              <label htmlFor="free-max-pages">
                <span>单篇页数上限（可选）</span>
                <span className="ac-hint">留空则沿用全局页数上限</span>
              </label>
              <input
                id="free-max-pages"
                type="number"
                min={1}
                max={2000}
                placeholder="留空不限"
                value={freeDraft.max_pages}
                onChange={(event) => {
                  setFreeDirty(true);
                  setFreeDraft({ ...freeDraft, max_pages: event.target.value });
                }}
              />
            </div>

            <div className="ac-field">
              <label htmlFor="free-notice">前台免费模式提示文案</label>
              <input
                id="free-notice"
                type="text"
                maxLength={500}
                value={freeDraft.notice}
                onChange={(event) => {
                  setFreeDirty(true);
                  setFreeDraft({ ...freeDraft, notice: event.target.value });
                }}
              />
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {freeDraft.providers.map((provider, index) => {
              const diag = testDiags[`free:${provider.id}`];
              const endpointChanged =
                provider.serverSecret.configured &&
                provider.api_key.action === "keep" &&
                (provider.base_url.trim() !== provider.originalUrl ||
                  provider.protocol !== provider.originalProtocol);
              return (
                <article key={provider.id} className="ac-provider-card">
                  <div className="ac-provider-hd">
                    <div className="ac-actions">
                      <span className={`ac-badge mono ${index === 0 ? "pen" : "warn"}`}>
                        #{index + 1} {index === 0 ? "主力" : "备用"}
                      </span>
                      <strong>{provider.name || provider.id}</strong>
                      <label className="ac-toggle">
                        <input
                          type="checkbox"
                          checked={provider.enabled}
                          onChange={(event) => updateProvider(index, { enabled: event.target.checked })}
                        />
                        <span>{provider.enabled ? "启用" : "停用"}</span>
                      </label>
                    </div>
                    <div className="ac-actions">
                      <button
                        type="button"
                        className="ac-btn sm"
                        disabled={index === 0}
                        onClick={() => moveFreeProvider(index, -1)}
                      >
                        上移
                      </button>
                      <button
                        type="button"
                        className="ac-btn sm"
                        disabled={index === freeDraft.providers.length - 1}
                        onClick={() => moveFreeProvider(index, 1)}
                      >
                        下移
                      </button>
                      <button
                        type="button"
                        className="ac-btn sm"
                        disabled={diag?.state === "busy" || freeSafetyBlockedIds.includes(provider.id)}
                        onClick={() => {
                          if (freeSafetyBlockedIds.includes(provider.id)) return;
                          void runConnectionTest(`free:${provider.id}`, {
                            target: "free_provider",
                            provider_id: provider.id,
                            protocol: provider.protocol,
                            base_url: provider.base_url.trim(),
                            model: provider.model.trim(),
                            timeout_s: Number(provider.timeout_s) || 65,
                            api_key: provider.api_key,
                          });
                        }}
                      >
                        {diag?.state === "busy" ? "测试中…" : "测试连接"}
                      </button>
                      <button
                        type="button"
                        className="ac-btn sm danger"
                        onClick={() => removeFreeProvider(index)}
                      >
                        删除渠道
                      </button>
                    </div>
                  </div>

                  <div className="ac-grid-2">
                    <div className="ac-field">
                      <label htmlFor={`provider-name-${provider.id}`}>渠道名称</label>
                      <input
                        id={`provider-name-${provider.id}`}
                        type="text"
                        maxLength={60}
                        value={provider.name}
                        onChange={(event) => updateProvider(index, { name: event.target.value })}
                      />
                    </div>

                    <div className="ac-field">
                      <label htmlFor={`provider-protocol-${provider.id}`}>接口协议</label>
                      <select
                        id={`provider-protocol-${provider.id}`}
                        value={provider.protocol}
                        onChange={(event) =>
                          updateProvider(index, { protocol: event.target.value as "openai" | "anthropic" })
                        }
                      >
                        <option value="openai">OpenAI 兼容</option>
                        <option value="anthropic">Anthropic 兼容</option>
                      </select>
                    </div>

                    <div className="ac-field span-2">
                      <label htmlFor={`provider-url-${provider.id}`}>
                        <span>服务地址（HTTPS · 禁重定向）</span>
                      </label>
                      <input
                        id={`provider-url-${provider.id}`}
                        type="url"
                        className="mono"
                        value={provider.base_url}
                        onChange={(event) => updateProvider(index, { base_url: event.target.value })}
                      />
                    </div>

                    <div className="ac-field">
                      <label htmlFor={`provider-model-${provider.id}`}>主模型</label>
                      <input
                        id={`provider-model-${provider.id}`}
                        type="text"
                        className="mono"
                        value={provider.model}
                        onChange={(event) => updateProvider(index, { model: event.target.value })}
                      />
                    </div>

                    <div className="ac-field">
                      <label htmlFor={`provider-fallback-${provider.id}`}>同 Key 繁忙备选模型（可选）</label>
                      <input
                        id={`provider-fallback-${provider.id}`}
                        type="text"
                        className="mono"
                        value={provider.fallback_model}
                        onChange={(event) => updateProvider(index, { fallback_model: event.target.value })}
                      />
                    </div>

                    <div className="ac-field">
                      <label htmlFor={`provider-timeout-${provider.id}`}>请求超时（秒）</label>
                      <input
                        id={`provider-timeout-${provider.id}`}
                        type="number"
                        min={15}
                        max={300}
                        value={provider.timeout_s}
                        onChange={(event) => updateProvider(index, { timeout_s: event.target.value })}
                      />
                    </div>

                    <div className="ac-field span-2">
                      <label>独立 API Key（加密绑定上方服务地址与协议）</label>
                      <SecretEditor
                        label="密钥"
                        view={provider.serverSecret}
                        value={provider.api_key}
                        allowInheritEnv={false}
                        placeholder="输入该渠道的新 API Key"
                        onChange={(nextSecret) => updateProvider(index, { api_key: nextSecret })}
                      />
                      {endpointChanged && (
                        <span className="ac-field-err">
                          修改服务地址或协议时，必须同时选择「更换密钥」或「清除并禁用」。
                        </span>
                      )}
                    </div>
                  </div>

                  {diag && (
                    <span className={`ac-diag ${diag.state}`} role="status">
                      <span className="ac-dot" aria-hidden="true" />
                      {diag.message}
                    </span>
                  )}
                </article>
              );
            })}

            {freeDraft.providers.length === 0 && (
              <p className="ac-empty">尚未配置任何免费翻译上游渠道，点击下方按钮添加。</p>
            )}
          </div>

          <div className="ac-card-ft">
            <button
              type="button"
              className="ac-btn sm"
              disabled={freeDraft.providers.length >= 10}
              onClick={addFreeProvider}
            >
              添加上游渠道
            </button>
            <div className="ac-actions">
              <button
                type="button"
                className="ac-btn sm"
                disabled={(!freeDirty && freeSafetyBlockedIds.length === 0) || freeSaving}
                onClick={() => {
                  abortAllTests();
                  setFreeDraft(buildFreeDraft(config));
                  setFreeDirty(false);
                  setFreeSafetyBlockedIds([]);
                  setFreeBanner(null);
                }}
              >
                还原更改
              </button>
              <button
                type="button"
                className="ac-btn primary sm"
                disabled={freeSaving || freeSafetyBlockedIds.length > 0}
                onClick={() => void saveFreeTranslation()}
              >
                {freeSaving ? "保存中…" : "保存免费翻译池"}
              </button>
            </div>
          </div>
        </section>
      )}

      {/* 分区 2：版面解析 */}
      {activeTab === "parser" && (
        <section className="ac-card" aria-labelledby="svc-parser-heading">
          <div className="ac-card-hd">
            <div>
              <h2 className="ac-card-title" id="svc-parser-heading">
                MinerU 云端版面解析
              </h2>
              <p className="ac-card-sub">支持自定义兼容端点、Token 热替换与加密存储网络代理。</p>
            </div>
            <span className={`ac-badge ${config.parser.mineru_token.configured ? "ok" : "warn"}`}>
              {config.parser.mineru_token.configured ? "已就绪" : "待配置 Token"}
            </span>
          </div>

          {renderBanner("parser", parserBanner)}

          <div className="ac-grid-2" style={{ marginTop: parserBanner ? 12 : 0 }}>
            <div className="ac-field span-2">
              <label htmlFor="mineru-base-url">
                <span>MinerU 接口地址</span>
                <span className="ac-hint">默认官方地址 https://mineru.net，也支持自建兼容服务</span>
              </label>
              <input
                id="mineru-base-url"
                type="url"
                className="mono"
                value={parserDraft.mineru_base_url}
                onChange={(event) => patchParser({ mineru_base_url: event.target.value }, "mineru")}
              />
            </div>

            <div className="ac-field span-2">
              <label>MinerU API Token</label>
              <SecretEditor
                label="Token"
                view={config.parser.mineru_token}
                value={parserDraft.mineru_token}
                allowInheritEnv
                placeholder="输入新的 MinerU API Token"
                onChange={(next) => patchParser({ mineru_token: next }, "mineru")}
              />
            </div>

            <div className="ac-field span-2">
              <details className="ac-fold">
                <summary>
                  <span>网络代理（加密存储）</span>
                  <span className="ac-badge mono">
                    {config.parser.mineru_proxy.mask ||
                      (config.parser.mineru_proxy.mode === "disabled" ? "已禁用" : "未配置")}
                  </span>
                </summary>
                <SecretEditor
                  label="代理"
                  view={config.parser.mineru_proxy}
                  value={parserDraft.mineru_proxy}
                  allowInheritEnv
                  placeholder="支持 http / https / socks5 / socks5h 含账密地址"
                  onChange={(next) => patchParser({ mineru_proxy: next }, "mineru")}
                />
              </details>
            </div>
          </div>

          <div className="ac-actions" style={{ marginTop: 12, justifyContent: "space-between" }}>
            <div>
              {mineruDiag && (
                <span className={`ac-diag ${mineruDiag.state}`} role="status">
                  <span className="ac-dot" aria-hidden="true" />
                  {mineruDiag.message}
                </span>
              )}
            </div>
            <button
              type="button"
              className="ac-btn sm"
              disabled={mineruDiag?.state === "busy" || parserSafetyBlocked}
              onClick={() => {
                if (parserSafetyBlocked) return;
                void runConnectionTest("parser:mineru", {
                  target: "mineru",
                  base_url: parserDraft.mineru_base_url.trim(),
                  token: parserDraft.mineru_token,
                  proxy: parserDraft.mineru_proxy,
                });
              }}
            >
              {mineruDiag?.state === "busy" ? "测试中…" : "测试解析连接"}
            </button>
          </div>

          <details className="ac-fold" style={{ marginTop: 16 }}>
            <summary>
              <span>PaddleOCR-VL（备用版面解析引擎）</span>
              <span className={`ac-badge ${parserDraft.paddle_public_enabled ? "ok" : ""}`}>
                {parserDraft.paddle_public_enabled ? "已向前台开放" : "前台未开放"}
              </span>
            </summary>
            <div className="ac-grid-2">
              <div className="ac-field span-2">
                <label className="ac-toggle">
                  <input
                    type="checkbox"
                    checked={parserDraft.paddle_public_enabled}
                    onChange={(event) =>
                      patchParser({ paddle_public_enabled: event.target.checked }, "paddle")
                    }
                  />
                  <span>向前台开放 PaddleOCR-VL 解析选项</span>
                </label>
              </div>
              <div className="ac-field span-2">
                <label>PaddleOCR Token</label>
                <SecretEditor
                  label="Token"
                  view={config.parser.paddle_token}
                  value={parserDraft.paddle_token}
                  allowInheritEnv
                  placeholder="输入 PaddleOCR API Token"
                  onChange={(next) => patchParser({ paddle_token: next }, "paddle")}
                />
              </div>
              <div className="ac-field span-2">
                <details className="ac-fold">
                  <summary>
                    <span>PaddleOCR 网络代理（加密存储）</span>
                    <span className="ac-badge mono">
                      {config.parser.paddle_proxy.mask ||
                        (config.parser.paddle_proxy.mode === "disabled" ? "已禁用" : "未配置")}
                    </span>
                  </summary>
                  <SecretEditor
                    label="代理"
                    view={config.parser.paddle_proxy}
                    value={parserDraft.paddle_proxy}
                    allowInheritEnv
                    placeholder="支持 http / https / socks5 / socks5h 含账密地址"
                    onChange={(next) => patchParser({ paddle_proxy: next }, "paddle")}
                  />
                </details>
              </div>
              <div className="ac-field span-2">
                <div className="ac-actions" style={{ justifyContent: "space-between" }}>
                  <div>
                    {paddleDiag && (
                      <span className={`ac-diag ${paddleDiag.state}`} role="status">
                        <span className="ac-dot" aria-hidden="true" />
                        {paddleDiag.message}
                      </span>
                    )}
                  </div>
                  <button
                    type="button"
                    className="ac-btn sm"
                    disabled={paddleDiag?.state === "busy"}
                    onClick={() =>
                      void runConnectionTest("parser:paddle", {
                        target: "paddle",
                        token: parserDraft.paddle_token,
                        proxy: parserDraft.paddle_proxy,
                      })
                    }
                  >
                    {paddleDiag?.state === "busy" ? "测试中…" : "测试 PaddleOCR 连接"}
                  </button>
                </div>
              </div>
            </div>
          </details>

          <div className="ac-card-ft">
            <span className="ac-hint">保存将同时生效 MinerU 与 PaddleOCR 设置，携带乐观锁版本号。</span>
            <div className="ac-actions">
              <button
                type="button"
                className="ac-btn sm"
                disabled={(!parserDirty && !parserSafetyBlocked) || parserSaving}
                onClick={() => {
                  abortAllTests();
                  setParserDraft(buildParserDraft(config));
                  setParserDirty(false);
                  setParserSafetyBlocked(false);
                  setParserBanner(null);
                }}
              >
                还原更改
              </button>
              <button
                type="button"
                className="ac-btn primary sm"
                disabled={parserSaving || parserSafetyBlocked}
                onClick={() => void saveParser()}
              >
                {parserSaving ? "保存中…" : "保存版面解析"}
              </button>
            </div>
          </div>
        </section>
      )}

      {/* 分区 3：自备预设与计价（两个独立可保存区块） */}
      {activeTab === "pricing" && (
        <>
          <section className="ac-card" aria-labelledby="svc-llm-heading">
            <div className="ac-card-hd">
              <div>
                <h2 className="ac-card-title" id="svc-llm-heading">
                  前台默认推荐与服务商快捷预设
                </h2>
                <p className="ac-card-sub">管理自备 Key 模式的默认地址、协议、推荐模型与快捷预设列表。</p>
              </div>
            </div>

            <div className="ac-alert info" style={{ marginBottom: 12 }}>
              <span>
                安全提示：后台绝不保存访客密钥；修改默认推荐地址时不会自动迁移环境变量中的共享密钥。
              </span>
            </div>

            {renderBanner("llm", llmBanner)}

            <div className="ac-grid-3" style={{ marginTop: llmBanner ? 12 : 0, marginBottom: 14 }}>
              <div className="ac-field">
                <label htmlFor="default-llm-url">默认推荐地址</label>
                <input
                  id="default-llm-url"
                  type="url"
                  className="mono"
                  value={llmDraft.base_url}
                  onChange={(event) => {
                    setLlmDirty(true);
                    setLlmDraft({ ...llmDraft, base_url: event.target.value });
                  }}
                />
              </div>
              <div className="ac-field">
                <label htmlFor="default-llm-protocol">默认协议</label>
                <select
                  id="default-llm-protocol"
                  value={llmDraft.protocol}
                  onChange={(event) => {
                    setLlmDirty(true);
                    setLlmDraft({ ...llmDraft, protocol: event.target.value as "openai" | "anthropic" });
                  }}
                >
                  <option value="openai">OpenAI 兼容</option>
                  <option value="anthropic">Anthropic 兼容</option>
                </select>
              </div>
              <div className="ac-field">
                <label htmlFor="default-llm-model">默认模型</label>
                <input
                  id="default-llm-model"
                  type="text"
                  className="mono"
                  value={llmDraft.model}
                  onChange={(event) => {
                    setLlmDirty(true);
                    setLlmDraft({ ...llmDraft, model: event.target.value });
                  }}
                />
              </div>
            </div>

            <div className="ac-actions" style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <strong>快捷服务商预设列表</strong>
              <button
                type="button"
                className="ac-btn sm"
                onClick={() => {
                  setLlmDirty(true);
                  setLlmDraft({
                    ...llmDraft,
                    presets: [
                      ...llmDraft.presets,
                      {
                        id: `preset-${llmDraft.presets.length + 1}`,
                        name: "新服务商",
                        protocol: "openai",
                        base_url: "https://",
                        modelsText: "",
                      },
                    ],
                  });
                }}
              >
                新增预设
              </button>
            </div>

            <table className="ac-table">
              <thead>
                <tr>
                  <th>标识</th>
                  <th>显示名称</th>
                  <th>协议</th>
                  <th>服务地址</th>
                  <th>推荐模型（逗号分隔）</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {llmDraft.presets.map((preset, index) => (
                  <tr key={`${preset.id}-${index}`}>
                    <td>
                      <input
                        aria-label={`预设标识 ${index + 1}`}
                        value={preset.id}
                        onChange={(event) => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.map((p, idx) =>
                              idx === index ? { ...p, id: event.target.value } : p,
                            ),
                          });
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`预设名称 ${index + 1}`}
                        value={preset.name}
                        onChange={(event) => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.map((p, idx) =>
                              idx === index ? { ...p, name: event.target.value } : p,
                            ),
                          });
                        }}
                      />
                    </td>
                    <td>
                      <select
                        aria-label={`预设协议 ${index + 1}`}
                        value={preset.protocol}
                        onChange={(event) => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.map((p, idx) =>
                              idx === index
                                ? { ...p, protocol: event.target.value as "openai" | "anthropic" }
                                : p,
                            ),
                          });
                        }}
                      >
                        <option value="openai">OpenAI</option>
                        <option value="anthropic">Anthropic</option>
                      </select>
                    </td>
                    <td>
                      <input
                        aria-label={`预设地址 ${index + 1}`}
                        value={preset.base_url}
                        onChange={(event) => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.map((p, idx) =>
                              idx === index ? { ...p, base_url: event.target.value } : p,
                            ),
                          });
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`预设模型 ${index + 1}`}
                        value={preset.modelsText}
                        onChange={(event) => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.map((p, idx) =>
                              idx === index ? { ...p, modelsText: event.target.value } : p,
                            ),
                          });
                        }}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="ac-btn sm danger"
                        onClick={() => {
                          setLlmDirty(true);
                          setLlmDraft({
                            ...llmDraft,
                            presets: llmDraft.presets.filter((_, idx) => idx !== index),
                          });
                        }}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="ac-card-ft">
              <span className="ac-hint">单独保存到 default_llm 分区。</span>
              <div className="ac-actions">
                <button
                  type="button"
                  className="ac-btn sm"
                  disabled={!llmDirty || llmSaving}
                  onClick={() => {
                    setLlmDraft(buildDefaultLlmDraft(config.default_llm));
                    setLlmDirty(false);
                    setLlmBanner(null);
                  }}
                >
                  还原预设
                </button>
                <button
                  type="button"
                  className="ac-btn primary sm"
                  disabled={llmSaving}
                  onClick={() => void saveDefaultLlm()}
                >
                  {llmSaving ? "保存中…" : "保存默认推荐与预设"}
                </button>
              </div>
            </div>
          </section>

          <section className="ac-card" aria-labelledby="svc-pricing-heading">
            <div className="ac-card-hd">
              <div>
                <h2 className="ac-card-title" id="svc-pricing-heading">
                  模型单价热表（元 / 百万 Token）
                </h2>
                <p className="ac-card-sub">覆盖内置单价，服务商调价时免发版实时同步费用预估与计费。</p>
              </div>
              <button
                type="button"
                className="ac-btn sm"
                onClick={() => {
                  setPricingDirty(true);
                  setPricingBanner(null);
                  setPricingDraft([
                    ...pricingDraft,
                    { model: "", cache_hit: "0", cache_miss: "0", output: "0", peak_factor: "1" },
                  ]);
                }}
              >
                新增模型单价
              </button>
            </div>

            {renderBanner("pricing", pricingBanner)}

            <table className="ac-table" style={{ marginTop: pricingBanner ? 12 : 0 }}>
              <thead>
                <tr>
                  <th>模型名称</th>
                  <th>缓存命中</th>
                  <th>缓存未中</th>
                  <th>输出</th>
                  <th>高峰倍率</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {pricingDraft.map((row, index) => (
                  <tr key={index}>
                    <td>
                      <input
                        aria-label={`计价模型 ${index + 1}`}
                        placeholder="如 deepseek-flash"
                        value={row.model}
                        onChange={(event) => {
                          setPricingDirty(true);
                          setPricingDraft(
                            pricingDraft.map((item, idx) =>
                              idx === index ? { ...item, model: event.target.value } : item,
                            ),
                          );
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`缓存命中单价 ${index + 1}`}
                        type="number"
                        step="0.01"
                        min={0}
                        value={row.cache_hit}
                        onChange={(event) => {
                          setPricingDirty(true);
                          setPricingDraft(
                            pricingDraft.map((item, idx) =>
                              idx === index ? { ...item, cache_hit: event.target.value } : item,
                            ),
                          );
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`缓存未中单价 ${index + 1}`}
                        type="number"
                        step="0.01"
                        min={0}
                        value={row.cache_miss}
                        onChange={(event) => {
                          setPricingDirty(true);
                          setPricingDraft(
                            pricingDraft.map((item, idx) =>
                              idx === index ? { ...item, cache_miss: event.target.value } : item,
                            ),
                          );
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`输出单价 ${index + 1}`}
                        type="number"
                        step="0.01"
                        min={0}
                        value={row.output}
                        onChange={(event) => {
                          setPricingDirty(true);
                          setPricingDraft(
                            pricingDraft.map((item, idx) =>
                              idx === index ? { ...item, output: event.target.value } : item,
                            ),
                          );
                        }}
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`高峰倍率 ${index + 1}`}
                        type="number"
                        step="0.1"
                        min={1}
                        max={10}
                        value={row.peak_factor}
                        onChange={(event) => {
                          setPricingDirty(true);
                          setPricingDraft(
                            pricingDraft.map((item, idx) =>
                              idx === index ? { ...item, peak_factor: event.target.value } : item,
                            ),
                          );
                        }}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="ac-btn sm danger"
                        onClick={() => {
                          setPricingDirty(true);
                          setPricingDraft(pricingDraft.filter((_, idx) => idx !== index));
                        }}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="ac-card-ft">
              <span className="ac-hint">未列入此表的模型将显示「按服务商计费」，独立保存 pricing 分区。</span>
              <div className="ac-actions">
                <button
                  type="button"
                  className="ac-btn sm"
                  disabled={!pricingDirty || pricingSaving}
                  onClick={() => {
                    setPricingDraft(buildPricingDraft(config));
                    setPricingDirty(false);
                    setPricingBanner(null);
                  }}
                >
                  还原计价
                </button>
                <button
                  type="button"
                  className="ac-btn primary sm"
                  disabled={pricingSaving}
                  onClick={() => void savePricing()}
                >
                  {pricingSaving ? "保存中…" : "保存模型单价"}
                </button>
              </div>
            </div>
          </section>
        </>
      )}

      {/* 分区 4：公共术语库 */}
      {activeTab === "glossary" && (
        <section className="ac-card" aria-labelledby="svc-glossary-heading">
          <div className="ac-card-hd">
            <div>
              <h2 className="ac-card-title" id="svc-glossary-heading">
                公共学术术语库（按顺序优先级合并）
              </h2>
              <p className="ac-card-sub">
                排在前面的来源优先覆盖同名术语；保存后后台异步重载并原子替换词库，不阻塞在线翻译。
              </p>
            </div>
            <div className="ac-actions">
              <span className={`ac-badge mono ${config.glossary.reloading ? "pen" : "ok"}`}>
                {config.glossary.reloading
                  ? "后台异步重载中"
                  : `已加载 ${config.glossary.loaded_terms.toLocaleString("zh-CN")} 词条`}
              </span>
            </div>
          </div>

          {renderBanner("glossary", glossaryBanner)}

          <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: glossaryBanner ? 12 : 0 }}>
            {glossaryOrder.map((id, index) => {
              const source = sourceMap.get(id);
              if (!source) return null;
              const checked = glossaryEnabled.includes(id);
              const isNonCommercial = !source.commercial_ok || source.id === "aitd";
              return (
                <div key={source.id} className="ac-gloss-row">
                  <div className="ac-gloss-left">
                    <input
                      id={`glossary-src-${source.id}`}
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleGlossarySource(source.id)}
                    />
                    <div>
                      <label htmlFor={`glossary-src-${source.id}`} style={{ fontWeight: 600, cursor: "pointer" }}>
                        {source.name}（<code className="ac-mono">{source.id}</code>）
                      </label>
                      <div className="ac-gloss-meta">
                        优先级 #{index + 1} · 许可协议：{source.license} ·{" "}
                        {source.cached ? "本地已缓存" : "未缓存（启用后异步下载）"}
                      </div>
                    </div>
                  </div>
                  <div className="ac-actions">
                    {isNonCommercial ? (
                      <span className="ac-badge warn">不可商用 · {source.license}</span>
                    ) : (
                      <span className="ac-badge ok">可商用 · {source.license}</span>
                    )}
                    <button
                      type="button"
                      className="ac-btn sm"
                      disabled={index === 0}
                      onClick={() => moveGlossarySource(index, -1)}
                    >
                      上移
                    </button>
                    <button
                      type="button"
                      className="ac-btn sm"
                      disabled={index === glossaryOrder.length - 1}
                      onClick={() => moveGlossarySource(index, 1)}
                    >
                      下移
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="ac-card-ft">
            <button
              type="button"
              className="ac-btn sm"
              disabled={glossaryReloadingReq}
              onClick={() => void reloadGlossaryNow()}
            >
              {glossaryReloadingReq ? "触发中…" : "立即重新加载"}
            </button>
            <div className="ac-actions">
              <button
                type="button"
                className="ac-btn sm"
                disabled={!glossaryDirty || glossarySaving}
                onClick={() => {
                  setGlossaryOrder(buildGlossaryOrder(config.glossary));
                  setGlossaryEnabled([...config.glossary.sources]);
                  setGlossaryDirty(false);
                  setGlossaryBanner(null);
                }}
              >
                还原更改
              </button>
              <button
                type="button"
                className="ac-btn primary sm"
                disabled={glossarySaving}
                onClick={() => void saveGlossary()}
              >
                {glossarySaving ? "保存中…" : "保存并异步重载"}
              </button>
            </div>
          </div>
        </section>
      )}
    </>
  );
}
