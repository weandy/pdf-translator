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
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const protocol = form.protocol || config?.default_protocol || "openai";
  const [models, setModels] = useState<{ list: string[]; error: string | null; busy: boolean }>({
    list: [],
    error: null,
    busy: false,
  });
  const [check, setCheck] = useState<LlmCheck | "busy" | null>(null);
  const fetched = useRef(""); // 上次获取模型列表时的协议、地址与 Key：没变就不重复请求（重复请求会让布局跳动、点不中按钮）
  // 用面板里还没保存的值请求；没填 Key 且用的是本站默认服务时，后端用本站的共享 Key
  const headers = () => authHeaders({ ...form, protocol });
  const canAsk = Boolean(form.llmKey || (config?.server_has_llm_key && !form.baseUrl && !form.protocol));

  const loadModels = async (next?: Settings) => {
    const f = next ?? form;
    if (!(f.llmKey || (config?.server_has_llm_key && !f.baseUrl && !f.protocol))) return;
    const signature = [f.protocol || protocol, f.baseUrl, f.llmKey].join("\n");
    if (signature === fetched.current) return;
    fetched.current = signature;
    setModels((m) => ({ ...m, busy: true }));
    try {
      const list = await api.llmModels(authHeaders({ ...f, protocol: f.protocol || protocol }));
      setModels({ list, error: null, busy: false });
    } catch (e) {
      setModels({ list: [], error: e instanceof Error ? e.message : String(e), busy: false });
    }
  };
  const runCheck = async () => {
    setCheck("busy");
    try {
      setCheck(await api.llmCheck(headers()));
    } catch (e) {
      setCheck({
        ok: false,
        model: form.model,
        latency_ms: 0,
        reply: "",
        detail: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => setForm((f) => ({ ...f, [key]: value }));
  const submit = (e: SubmitEvent<HTMLFormElement>) => {
    e.preventDefault();
    saveSettings(form);
    onClose();
  };

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-head">
          <h2 id="settings-title">设置</h2>
          <button type="button" className="quiet" onClick={onClose}>
            关闭
          </button>
        </div>
        <p className="hint">
          Key 只保存在这个浏览器里。翻译时随请求交给本站后端转发给服务商，后端不保存。不要在公用电脑上保存 Key。
        </p>

        <section>
          <h3>翻译服务</h3>
          <p className="hint">
            支持 OpenAI 兼容接口（默认 DeepSeek）与 Anthropic 接口（Claude，或 DeepSeek 等提供的兼容地址）。
          </p>
          <label className="field">
            <span>接口协议</span>
            <select
              value={protocol}
              onChange={(e) => {
                const next = { ...form, protocol: e.target.value as Settings["protocol"] };
                setForm(next);
                setCheck(null);
                void loadModels(next);
              }}
            >
              <option value="openai">OpenAI 兼容（DeepSeek、通义千问、GLM、Ollama 等）</option>
              <option value="anthropic">Anthropic（Claude）</option>
            </select>
          </label>
          <label className="field">
            <span>API Key</span>
            <input
              type="password"
              autoComplete="off"
              value={form.llmKey}
              onChange={(e) => set("llmKey", e.target.value.trim())}
              onBlur={() => void loadModels()}
              placeholder={config?.server_has_llm_key ? "留空则使用本站提供的 Key" : "sk-..."}
            />
          </label>
          <label className="field">
            <span>接口地址</span>
            <input
              value={form.baseUrl}
              onChange={(e) => set("baseUrl", e.target.value.trim())}
              onBlur={() => void loadModels()}
              placeholder={
                form.protocol && form.protocol !== config?.default_protocol
                  ? BASE_URLS[protocol]
                  : (config?.default_base_url ?? BASE_URLS[protocol])
              }
            />
          </label>
          <label className="field">
            <span>
              模型
              {models.busy && <small className="muted">正在获取模型列表…</small>}
              {!models.busy && models.list.length > 0 && (
                <small className="muted">已获取 {models.list.length} 个模型，可直接选择</small>
              )}
            </span>
            <input
              value={form.model}
              list="model-options"
              onChange={(e) => set("model", e.target.value.trim())}
              onFocus={() => models.list.length === 0 && !models.busy && void loadModels()}
              placeholder={config?.default_model ?? "deepseek-flash"}
            />
            <datalist id="model-options">
              {models.list.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>
          {models.error && <p className="hint">{models.error}</p>}
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
        </section>

        <section>
          <h3>版面解析</h3>
          <label className="field">
            <span>MinerU Token</span>
            <input
              type="password"
              autoComplete="off"
              value={form.parserKey}
              onChange={(e) => set("parserKey", e.target.value.trim())}
              placeholder={config?.server_has_parser_token ? "留空则使用本站提供的 Token" : "在 mineru.net 免费申请"}
            />
          </label>
          <p className="hint">
            在{" "}
            <a href="https://mineru.net/apiManage" target="_blank" rel="noreferrer">
              mineru.net
            </a>{" "}
            免费申请，每天 1000 页优先额度。
          </p>
        </section>

        <section>
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
          <label className="choice">
            <input
              type="checkbox"
              checked={form.rememberKeys}
              onChange={(e) => set("rememberKeys", e.target.checked)}
            />
            <span>
              在这个浏览器里记住 Key
              <small>不勾选时，关闭页面后需要重新填写。</small>
            </span>
          </label>
        </section>

        <section>
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
        </section>

        {config && <p className="hint">使用的开源组件：{config.attributions.join("；")}</p>}

        <div className="dialog-actions">
          <button
            type="button"
            className="quiet danger"
            onClick={() => {
              clearKeys();
              setForm((f) => ({ ...f, llmKey: "", parserKey: "" }));
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
