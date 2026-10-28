import { useQuery } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";

import { clearKeys, saveSettings, useSettings, type Settings } from "@/entities/settings/model";
import { api } from "@/shared/api/client";

interface Props {
  onClose: () => void;
}

export function SettingsPanel({ onClose }: Props) {
  const saved = useSettings();
  const [form, setForm] = useState<Settings>(saved);
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });

  const set = <K extends keyof Settings>(key: K, value: Settings[K]) => setForm((f) => ({ ...f, [key]: value }));
  const submit = (e: SubmitEvent<HTMLFormElement>) => {
    e.preventDefault();
    saveSettings(form);
    onClose();
  };

  return (
    <div className="dialog-backdrop" role="presentation" onClick={onClose}>
      <form className="dialog" role="dialog" aria-label="设置" onSubmit={submit} onClick={(e) => e.stopPropagation()}>
        <h2>设置</h2>
        <p className="hint">
          Key 只保存在当前浏览器，随请求发给本站后端转交服务商，后端不保存。不要在公用电脑上保存 Key。
        </p>

        <fieldset>
          <legend>翻译服务（OpenAI 兼容接口）</legend>
          <label>
            接口地址
            <input
              value={form.baseUrl}
              onChange={(e) => set("baseUrl", e.target.value.trim())}
              placeholder={config?.default_base_url ?? "https://api.deepseek.com"}
            />
          </label>
          <label>
            模型
            <input
              value={form.model}
              onChange={(e) => set("model", e.target.value.trim())}
              placeholder={config?.default_model ?? "deepseek-flash"}
            />
          </label>
          <label>
            API Key
            <input
              type="password"
              autoComplete="off"
              value={form.llmKey}
              onChange={(e) => set("llmKey", e.target.value.trim())}
              placeholder={config?.server_has_llm_key ? "留空则使用本站提供的 Key" : "sk-..."}
            />
          </label>
        </fieldset>

        <fieldset>
          <legend>版面解析（MinerU 云端）</legend>
          <label>
            MinerU Token
            <input
              type="password"
              autoComplete="off"
              value={form.parserKey}
              onChange={(e) => set("parserKey", e.target.value.trim())}
              placeholder={config?.server_has_parser_token ? "留空则使用本站提供的 Token" : "在 mineru.net 免费申请"}
            />
          </label>
          <p className="hint">
            免费申请：
            <a href="https://mineru.net/apiManage" target="_blank" rel="noreferrer">
              mineru.net
            </a>
            （每天 1000 页优先额度）
          </p>
        </fieldset>

        <fieldset>
          <legend>默认选项</legend>
          <label className="inline">
            <input
              type="radio"
              checked={form.quality === "default"}
              onChange={() => set("quality", "default")}
            />
            默认（不思考，快且便宜）
          </label>
          <label className="inline">
            <input type="radio" checked={form.quality === "careful"} onChange={() => set("quality", "careful")} />
            精翻（开启低强度思考，约 4 倍费用）
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={form.includeReferences}
              onChange={(e) => set("includeReferences", e.target.checked)}
            />
            翻译参考文献
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={form.rememberKeys}
              onChange={(e) => set("rememberKeys", e.target.checked)}
            />
            在此浏览器中记住 Key
          </label>
        </fieldset>

        <div className="actions">
          <button
            type="button"
            className="danger"
            onClick={() => {
              clearKeys();
              setForm((f) => ({ ...f, llmKey: "", parserKey: "" }));
            }}
          >
            清除已保存的 Key
          </button>
          <span className="spacer" />
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary">
            保存
          </button>
        </div>
        {config && <p className="hint small">使用的开源组件：{config.attributions.join("；")}</p>}
      </form>
    </div>
  );
}
