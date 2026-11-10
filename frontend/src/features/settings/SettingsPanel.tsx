import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type SubmitEvent } from "react";

import { authHeaders, clearKeys, saveSettings, useSettings, type Settings } from "@/entities/settings/model";
import { api, type LlmCheck } from "@/shared/api/client";

const BASE_URLS = { openai: "https://api.deepseek.com", anthropic: "https://api.anthropic.com" } as const;

interface Props {
  onClose: () => void;
}

export function SettingsPanel({ onClose }: Props) {
  const saved = useSettings();
  const [form, setForm] = useState<Settings>(saved);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });

  const requestClose = () => {
    if (dirty && !window.confirm("设置还没有保存，确定要关闭吗？")) return;
    onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && requestClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const protocol = form.protocol || config?.default_protocol || "openai";
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
  // 用面板里还没保存的值请求；没填 Key 且用的是本站默认服务时，后端用本站的共享 Key
  const headers = () =>
    Object.fromEntries(Object.entries(authHeaders({ ...form, protocol })).filter(([key]) => key.startsWith("X-LLM-")));
  const canAsk = Boolean(form.llmKey || (config?.server_has_llm_key && !form.baseUrl && !form.protocol));
  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    if (key === "protocol" || key === "baseUrl" || key === "llmKey") {
      modelRequest.current?.abort();
      modelRequest.current = null;
      setModels({ list: [], error: null, busy: false });
    }
    if (key === "protocol" || key === "baseUrl" || key === "llmKey" || key === "model") {
      checkRequest.current?.abort();
      checkRequest.current = null;
      setCheck(null);
    }
    setForm((f) => ({ ...f, [key]: value }));
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
    saveSettings(form);
    onClose();
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
                    onChange={(e) => set("baseUrl", e.target.value.trim())}
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
              <details className="more">
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
              <small>不开启思考，快且便宜。论文类内容推荐用这个。</small>
            </span>
          </label>
          <label className="choice">
            <input type="radio" checked={form.quality === "careful"} onChange={() => set("quality", "careful")} />
            <span>
              精翻
              <small>开启低强度思考，费用约为默认的 4 倍。</small>
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

        {config && (
          <details className="group">
            <summary>使用的开源组件</summary>
            <p className="hint">{config.attributions.join("；")}</p>
          </details>
        )}

        <div className="dialog-actions">
          <button
            type="button"
            className="quiet danger"
            onClick={() => {
              clearKeys();
              set("llmKey", "");
              set("parserKey", "");
            }}
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
