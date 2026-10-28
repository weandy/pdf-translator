import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type SubmitEvent } from "react";

import { clearKeys, saveSettings, useSettings, type Settings } from "@/entities/settings/model";
import { api } from "@/shared/api/client";

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
          <p className="hint">任何 OpenAI 兼容接口都可以，默认使用 DeepSeek。</p>
          <label className="field">
            <span>API Key</span>
            <input
              type="password"
              autoComplete="off"
              value={form.llmKey}
              onChange={(e) => set("llmKey", e.target.value.trim())}
              placeholder={config?.server_has_llm_key ? "留空则使用本站提供的 Key" : "sk-..."}
            />
          </label>
          <label className="field">
            <span>接口地址</span>
            <input
              value={form.baseUrl}
              onChange={(e) => set("baseUrl", e.target.value.trim())}
              placeholder={config?.default_base_url ?? "https://api.deepseek.com"}
            />
          </label>
          <label className="field">
            <span>模型</span>
            <input
              value={form.model}
              onChange={(e) => set("model", e.target.value.trim())}
              placeholder={config?.default_model ?? "deepseek-flash"}
            />
          </label>
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
              checked={form.translateFigures}
              onChange={(e) => set("translateFigures", e.target.checked)}
            />
            <span>
              翻译图中文字
              <small>坐标轴、图例、框图里的文字译成中文后排回原位；图片格式的图暂不支持。</small>
            </span>
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
