import { useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, type SubmitEvent } from "react";

import {
  authHeaders,
  bindKeysForSave,
  clearKeys,
  confirmParserKeyBinding,
  effectiveNormalizedLlmTarget,
  getSettings,
  llmKeyNeedsRebind,
  parserKeyNeedsRebind,
  saveSettings,
  useSettings,
  type Settings,
} from "@/entities/settings/model";
import { api, type LlmCheck, type LlmPreset } from "@/shared/api/client";
import { normalizeEndpointUrl } from "@/entities/settings/target";
import { credentialEpoch, emptyCredentials, getMigrationError, useCredentialEpoch } from "@/entities/settings/vault";
import { useConfirm } from "@/shared/feedback/ConfirmDialog";
import { useToast } from "@/shared/feedback/Toast";

const BASE_URLS = { openai: "https://api.deepseek.com", anthropic: "https://api.anthropic.com" } as const;

interface Props {
  onClose: () => void;
}

export function SettingsPanel({ onClose }: Props) {
  const confirm = useConfirm();
  const toast = useToast();
  const saved = useSettings();
  const epoch = useCredentialEpoch();
  const [draftEpoch, setDraftEpoch] = useState(epoch);
  const [storageError, setStorageError] = useState(getMigrationError);
  const [form, setForm] = useState<Settings>(saved);
  const [keyClearedNotice, setKeyClearedNotice] = useState<string | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });

  const requestClose = async () => {
    if (dirty && !(await confirm({ title: "关闭设置", message: "设置还没有保存，确定要关闭吗？", confirmLabel: "放弃修改" }))) return;
    onClose();
  };
  const requestCloseRef = useRef(requestClose);
  useLayoutEffect(() => {
    requestCloseRef.current = requestClose;
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented && !e.isComposing) {
        e.preventDefault();
        void requestCloseRef.current();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const protocol = form.protocol || config?.default_protocol || "openai";
  const needsRebind = llmKeyNeedsRebind(form, config);
  const parserNeedsRebind = parserKeyNeedsRebind(form, config);
  const [models, setModels] = useState<{ list: string[]; error: string | null; busy: boolean; loaded?: boolean }>({
    list: [],
    error: null,
    busy: false,
  });
  const [check, setCheck] = useState<LlmCheck | "busy" | null>(null);
  const modelRequest = useRef<AbortController | null>(null);
  const checkRequest = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      modelRequest.current?.abort();
      checkRequest.current?.abort();
    },
    [],
  );
  useLayoutEffect(() => () => {
    modelRequest.current?.abort();
    checkRequest.current?.abort();
  }, [epoch]);
  if (draftEpoch !== epoch) {
    setDraftEpoch(epoch);
    const target = effectiveNormalizedLlmTarget(form, config);
    const restoredKey = target ? saved.llmKeysByTarget[target.key] ?? "" : "";
    setForm({
      ...form,
      ...emptyCredentials,
      rememberKeys: saved.rememberKeys,
      llmKey: restoredKey,
      llmKeyBoundUrl: restoredKey && target ? target.url : "",
      llmKeyBoundProtocol: restoredKey && target ? target.protocol : "",
      llmKeysByTarget: saved.llmKeysByTarget,
      parserKey: saved.parserKey,
      parserKeyBoundUrl: saved.parserKeyBoundUrl,
    });
    setModels({ list: [], error: null, busy: false });
    setCheck(null);
    setKeyClearedNotice("其他标签页已更改或清除已保存的 Key，表单中的旧凭据已同步更新");
  }
  const abortProbes = (resetModels: boolean, resetCheck: boolean) => {
    if (resetModels) {
      modelRequest.current?.abort();
      modelRequest.current = null;
      setModels({ list: [], error: null, busy: false });
    }
    if (resetCheck) {
      checkRequest.current?.abort();
      checkRequest.current = null;
      setCheck(null);
    }
  };
  // 用面板里还没保存的值请求；没填 Key 且用的是本站默认服务时，后端用本站的共享 Key
  const headers = () =>
    Object.fromEntries(Object.entries(authHeaders({ ...form, protocol }, config)).filter(([key]) => key.startsWith("X-LLM-")));
  // 只要个人 Key 处于待重新绑定状态，强制禁止探测，绝不借 server_has_llm_key 旁路放行
  const canAsk =
    !needsRebind &&
    Boolean(form.llmKey.trim() || (config?.server_has_llm_key && !form.baseUrl && !form.protocol));
  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    if (key === "protocol" || key === "baseUrl" || key === "llmKey") {
      abortProbes(true, true);
    } else if (key === "model") {
      abortProbes(false, true);
    }
    if (key === "llmKey") {
      const nextKey = value as string;
      setKeyClearedNotice(null);
      setForm((f) => {
        const target = effectiveNormalizedLlmTarget(f, config);
        const llmKeysByTarget = { ...f.llmKeysByTarget };
        if (target) {
          if (nextKey.trim()) llmKeysByTarget[target.key] = nextKey.trim();
          else delete llmKeysByTarget[target.key];
        }
        return {
          ...f,
          llmKey: nextKey,
          llmKeysByTarget,
          llmKeyBoundUrl: nextKey ? (target?.url ?? "") : "",
          llmKeyBoundProtocol: nextKey ? (target?.protocol ?? "") : "",
        };
      });
      return;
    }
    if (key === "parserKey") {
      const nextParserKey = value as string;
      setForm((f) => {
        if (!nextParserKey.trim()) {
          return { ...f, parserKey: nextParserKey, parserKeyBoundUrl: "" };
        }
        const defaultMineruUrl = config?.default_mineru_base_url?.trim() ?? "";
        return {
          ...f,
          parserKey: nextParserKey,
          parserKeyBoundUrl: normalizeEndpointUrl(defaultMineruUrl) ?? "",
        };
      });
      return;
    }
    if (key === "baseUrl" || key === "protocol") {
      switchTarget({ ...form, [key]: value });
      return;
    }
    setForm((f) => ({ ...f, [key]: value }));
  };
  const presets = config?.llm_presets ?? [];
  const switchTarget = (next: Settings) => {
    const before = effectiveNormalizedLlmTarget(form, config);
    const after = effectiveNormalizedLlmTarget(next, config);
    if (before?.key === after?.key && (before || (next.baseUrl === form.baseUrl && next.protocol === form.protocol))) {
      setForm(next);
      return;
    }
    const llmModelsByTarget = { ...form.llmModelsByTarget };
    if (before) llmModelsByTarget[before.key] = form.model;
    const llmKeysByTarget = { ...form.llmKeysByTarget };
    if (before && form.llmKey.trim() && !needsRebind) llmKeysByTarget[before.key] = form.llmKey.trim();
    const llmKey = after ? (llmKeysByTarget[after.key] ?? "") : "";
    const fallback = presets.find((preset) =>
      effectiveNormalizedLlmTarget({ protocol: preset.protocol, baseUrl: preset.base_url })?.key === after?.key
    )?.models?.[0] ?? "";
    setKeyClearedNotice(llmKey ? "已自动恢复该服务商已保存的 API Key" : "已切换服务商，请填写对应 API Key");
    setForm({
      ...next,
      llmKeysByTarget,
      llmModelsByTarget,
      model: after ? (llmModelsByTarget[after.key] ?? fallback) : "",
      llmKey,
      llmKeyBoundUrl: llmKey ? (after?.url ?? "") : "",
      llmKeyBoundProtocol: llmKey ? (after?.protocol ?? "") : "",
    });
  };
  const applyPreset = (preset: LlmPreset) => {
    abortProbes(true, true);
    switchTarget({ ...form, protocol: preset.protocol, baseUrl: preset.base_url });
  };
  const confirmCurrentTarget = () => {
    const target = effectiveNormalizedLlmTarget(form, config);
    if (!target) return;
    setKeyClearedNotice(null);
    setForm((f) => ({
      ...f,
      llmKeysByTarget: { ...f.llmKeysByTarget, [target.key]: f.llmKey.trim() },
      llmKeyBoundUrl: target.url,
      llmKeyBoundProtocol: target.protocol,
    }));
  };
  const confirmParserTarget = () => {
    if (!config) return;
    setForm((f) => confirmParserKeyBinding(f, config));
  };
  const sharedParser = Boolean(config?.server_has_parser_token);
  const parserFields = (
    <>
      <label className="field">
        <span>MinerU Token（版面解析{sharedParser ? "，可选" : "，必填"}）</span>
        <input
          type="password"
          autoComplete="off"
          value={form.parserKey}
          onChange={(e) => set("parserKey", e.target.value.trim())}
          placeholder={sharedParser ? "留空使用本站的版面解析服务" : "在 mineru.net 申请 API Token"}
        />
      </label>
      {parserNeedsRebind && config?.default_mineru_base_url && (
        <div className="key-rebind-notice" role="alert">
          <p>
            当前个人 MinerU Token 尚未绑定到当前解析服务地址（目标：{config.default_mineru_base_url}），请确认目标地址或清空后重新填写。
          </p>
          <div className="row-actions">
            <button type="button" onClick={confirmParserTarget}>
              确认发送到此地址
            </button>
            <button type="button" className="quiet danger" onClick={() => set("parserKey", "")}>
              清空旧 Token
            </button>
          </div>
        </div>
      )}
      <p className="hint">
        在{" "}
        <a href="https://mineru.net/apiManage" target="_blank" rel="noreferrer">
          MinerU API 管理
        </a>{" "}
        申请解析 API Token，额度与有效期以你的账号为准。
        {sharedParser && "填写后优先使用你的 Token，清空并保存即可恢复本站额度。"}
      </p>
    </>
  );

  const loadModels = async () => {
    if (!canAsk) return;
    modelRequest.current?.abort();
    const controller = new AbortController();
    modelRequest.current = controller;
    setModels({ list: [], error: null, busy: true });
    try {
      const list = await api.llmModels(headers(), controller.signal);
      // 即使接口已返回，用户也可能刚切换配置，旧服务的模型不能覆盖新配置。
      if (modelRequest.current !== controller || controller.signal.aborted) return;
      setModels({ list: [...new Set(list)].sort(), error: null, busy: false, loaded: true });
    } catch (e) {
      if (modelRequest.current !== controller || controller.signal.aborted) return;
      setModels({
        list: [],
        error: `${e instanceof Error ? e.message : String(e)}。可以重试，或手动填写模型名称。`,
        busy: false,
      });
    }
  };
  const runCheck = async () => {
    if (!canAsk) return;
    checkRequest.current?.abort();
    const controller = new AbortController();
    checkRequest.current = controller;
    setCheck("busy");
    try {
      const result = await api.llmCheck(headers(), controller.signal);
      if (checkRequest.current === controller && !controller.signal.aborted) setCheck(result);
    } catch (e) {
      if (checkRequest.current !== controller || controller.signal.aborted) return;
      setCheck({
        ok: false,
        model: form.model,
        latency_ms: 0,
        reply: "",
        detail: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const submit = (e: SubmitEvent<HTMLFormElement>) => {
    e.preventDefault();
    const target = effectiveNormalizedLlmTarget(form, config);
    const { cloudConsent, translationMode } = getSettings();
    const next = bindKeysForSave({
      ...form, cloudConsent, translationMode,
      llmModelsByTarget: target ? { ...form.llmModelsByTarget, [target.key]: form.model } : form.llmModelsByTarget,
    }, config);
    const result = saveSettings(next, draftEpoch);
    if (!result.ok) {
      setStorageError(result.error);
      toast.error(result.error);
      return;
    }
    toast.success("已保存设置");
    onClose();
  };

  const clearSavedKeys = async () => {
    if (!(await confirm({
      title: "清除已保存的 Key",
      message: "清除所有服务商的 API Key 和 MinerU Token？",
      confirmLabel: "全部清除",
      tone: "danger",
    })) || draftEpoch !== credentialEpoch) return;
    const result = clearKeys(draftEpoch);
    if (!result.ok) {
      setStorageError(result.error);
      toast.error(result.error);
      return;
    }
    abortProbes(true, true);
    setDraftEpoch(credentialEpoch);
    setStorageError(undefined);
    setKeyClearedNotice("已清除全部已保存的 Key");
    setForm((f) => ({ ...f, ...emptyCredentials }));
    toast.success("已清除全部已保存的 Key");
  };

  return (
    <div className="dialog-backdrop" role="presentation" onClick={requestClose}>
      <form
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-head">
          <h2 id="settings-title">设置 {dirty && <small className="unsaved-tag">未保存</small>}</h2>
          <button type="button" className="quiet" onClick={requestClose}>
            关闭
          </button>
        </div>
        <p className="hint">
          你填写的 Key 只保存在这个浏览器里，使用时随请求交给本站转发给服务商，本站不持久保存。
          {sharedParser && "本站提供的解析凭据由服务器管理，不会下发到浏览器。"}
        </p>

        <section>
          <h3>{sharedParser ? "填写翻译 API Key 即可开始" : "配置翻译与版面解析"}</h3>
          <div className="step">
            <span className="step-no" aria-hidden="true">
              1
            </span>
            <div className="step-body">
              {presets.length > 0 && (
                <div className="preset-bar" role="group" aria-label="常用服务商快捷预设">
                  <span className="preset-label">常用服务商快捷预设</span>
                  <div className="preset-list">
                    {presets.map((preset) => {
                      const active =
                        protocol === preset.protocol &&
                        (form.baseUrl === preset.base_url ||
                          (!form.baseUrl && config?.default_base_url === preset.base_url));
                      return (
                        <button
                          key={preset.id}
                          type="button"
                          className={active ? "preset-btn on" : "preset-btn"}
                          aria-pressed={active}
                          onClick={() => applyPreset(preset)}
                        >
                          {preset.name}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              <label className="field">
                <span>翻译服务 API Key</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={form.llmKey}
                  onChange={(e) => set("llmKey", e.target.value.trim())}
                  placeholder={config?.server_has_llm_key ? "留空则使用本站提供的 Key" : "sk-..."}
                />
              </label>
              {keyClearedNotice && (
                <p className="key-rebind-notice" role="status">
                  {keyClearedNotice}
                </p>
              )}
              {needsRebind && effectiveNormalizedLlmTarget(form, config) && (
                <div className="key-rebind-notice" role="alert">
                  <p>
                    当前个人 API Key 尚未绑定到当前服务地址（目标：{effectiveNormalizedLlmTarget(form, config)?.url}），请确认目标地址或重新填写后再测试或保存。
                  </p>
                  <div className="row-actions">
                    <button type="button" onClick={confirmCurrentTarget}>
                      确认发送到此地址
                    </button>
                    <button type="button" className="quiet danger" onClick={() => set("llmKey", "")}>
                      清空旧 Key
                    </button>
                  </div>
                </div>
              )}
              {form.llmKey && !needsRebind && !form.baseUrl && config?.default_base_url && (
                <p className="hint preset-target-hint" role="note">
                  当前未填写自定义接口地址，你的 API Key 将发往默认服务商地址：{config.default_base_url}
                </p>
              )}
              <p className="hint">
                默认使用 DeepSeek：在{" "}
                <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noreferrer">
                  DeepSeek 开放平台
                </a>{" "}
                创建 API Key，一篇论文约几分钱。
              </p>
              <div className="check-row">
                <button type="button" disabled={!canAsk || check === "busy"} onClick={() => void runCheck()}>
                  {check === "busy" ? "测试中…" : "测试连接"}
                </button>
                {check && check !== "busy" && (
                  <span className={check.ok ? "check-ok" : "error"} role="status">
                    {check.ok
                      ? `已连通：${check.model}，用时 ${(check.latency_ms / 1000).toFixed(1)} 秒`
                      : `连接失败：${check.detail}`}
                  </span>
                )}
              </div>
              <details className="more">
                <summary>用其他翻译服务（接口协议、地址、模型）</summary>
                <p className="hint">
                  支持 OpenAI 兼容接口（通义千问、GLM、Ollama 等）与 Anthropic 接口（Claude，或 DeepSeek
                  等提供的兼容地址）。
                </p>
                <label className="field">
                  <span>接口协议</span>
                  <select value={protocol} onChange={(e) => set("protocol", e.target.value as Settings["protocol"])}>
                    <option value="openai">OpenAI 兼容（DeepSeek、通义千问、GLM、Ollama 等）</option>
                    <option value="anthropic">Anthropic（Claude）</option>
                  </select>
                </label>
                <label className="field">
                  <span>接口地址</span>
                  <input
                    value={form.baseUrl}
                    onChange={(e) => set("baseUrl", e.target.value)}
                    placeholder={
                      form.protocol && form.protocol !== config?.default_protocol
                        ? BASE_URLS[protocol]
                        : (config?.default_base_url ?? BASE_URLS[protocol])
                    }
                  />
                </label>
                <div className="check-row">
                  <button type="button" disabled={!canAsk || models.busy} onClick={() => void loadModels()}>
                    {models.busy ? "获取中…" : models.list.length ? "重新获取模型" : "获取模型列表"}
                  </button>
                  <span className="hint" role="status" aria-live="polite">
                    {models.busy
                      ? "正在查询当前接口，最多等待 20 秒…"
                      : models.list.length
                        ? `已获取 ${models.list.length} 个模型`
                        : "填写接口地址和 API Key 后获取，无需先保存。"}
                  </span>
                </div>
                {models.list.length > 0 && (
                  <label className="field">
                    <span>从列表选择模型</span>
                    <select
                      value={models.list.includes(form.model) ? form.model : ""}
                      onChange={(e) => set("model", e.target.value)}
                    >
                      <option value="" disabled>
                        请选择模型
                      </option>
                      {models.list.map((m) => (
                        <option key={m} value={m}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label className="field">
                  <span>模型名称（也可手动填写）</span>
                  <input
                    value={form.model}
                    onChange={(e) => set("model", e.target.value.trim())}
                    placeholder={config?.default_model ?? "deepseek-flash"}
                  />
                </label>
                {models.error && (
                  <p className="hint" role="alert">
                    {models.error}
                  </p>
                )}
                {models.loaded && models.list.length === 0 && (
                  <p className="hint" role="status">
                    这个接口没有返回可选模型，请手动填写模型名称。
                  </p>
                )}
                <p className="hint">
                  列表只代表接口返回的模型，不保证当前 Key 有使用权限；选择后可点击“测试连接”验证。
                </p>
              </details>
            </div>
          </div>
          {sharedParser ? (
            <div className="step-body">
              <p className="hint" role="status">
                {form.parserKey
                  ? "当前选择：使用你自己的 MinerU Token。"
                  : "本站已提供 MinerU 版面解析，无需再填写 Token。"}
              </p>
              <details className="more" open={parserNeedsRebind || undefined}>
                <summary>使用自己的 MinerU Token（可选）</summary>
                {parserFields}
              </details>
            </div>
          ) : (
            <div className="step">
              <span className="step-no" aria-hidden="true">
                2
              </span>
              <div className="step-body">{parserFields}</div>
            </div>
          )}
        </section>

        <details className="group">
          <summary>翻译偏好</summary>
          <h3>翻译方式</h3>
          <label className="choice">
            <input type="radio" checked={form.quality === "default"} onChange={() => set("quality", "default")} />
            <span>
              默认
              <small>思考设置依服务商和模型支持而定，费用按实际用量计。</small>
            </span>
          </label>
          <label className="choice">
            <input type="radio" checked={form.quality === "careful"} onChange={() => set("quality", "careful")} />
            <span>
              精翻
              <small>在服务商和模型支持时尝试使用较高思考强度，费用按实际用量计。</small>
            </span>
          </label>
          <label className="choice">
            <input
              type="checkbox"
              checked={form.includeReferences}
              onChange={(e) => set("includeReferences", e.target.checked)}
            />
            <span>翻译参考文献</span>
          </label>
          <h3>双语对照</h3>
          <label className="choice">
            <input
              type="radio"
              checked={form.translationSide === "right"}
              onChange={() => set("translationSide", "right")}
            />
            <span>译文在右边</span>
          </label>
          <label className="choice">
            <input
              type="radio"
              checked={form.translationSide === "left"}
              onChange={() => set("translationSide", "left")}
            />
            <span>
              译文在左边
              <small>阅读器的“对照”模式按这里的设置排列；下载双语 PDF 时两种都可以选。</small>
            </span>
          </label>
          <h3>Key</h3>
          <label className="choice">
            <input
              type="checkbox"
              checked={form.rememberKeys}
              onChange={(e) => set("rememberKeys", e.target.checked)}
            />
            <span>
              在这个浏览器里记住 Key
              <small>不勾选时，关闭页面后需要重新填写。不要在公用电脑上保存 Key。</small>
            </span>
          </label>
        </details>

        {storageError && <p className="error" role="alert">{storageError}</p>}
        <div className="dialog-actions">
          <button
            type="button"
            className="quiet danger"
            onClick={() => void clearSavedKeys()}
          >
            清除已保存的 Key
          </button>
          <span className="spacer" />
          <button type="submit" className="primary">
            保存
          </button>
        </div>
      </form>
    </div>
  );
}
