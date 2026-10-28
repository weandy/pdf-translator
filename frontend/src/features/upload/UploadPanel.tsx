import { useQuery } from "@tanstack/react-query";
import { useState, type DragEvent } from "react";

import { useSettings } from "@/entities/settings/model";
import { api, type Job } from "@/shared/api/client";

interface Props {
  onCreated: (job: Job) => void;
  onNeedSettings: () => void;
}

export function UploadPanel({ onCreated, onNeedSettings }: Props) {
  const settings = useSettings();
  const { data: config } = useQuery({ queryKey: ["config"], queryFn: api.config });
  const [file, setFile] = useState<File | null>(null);
  const [pages, setPages] = useState<number | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: pages !== null,
  });

  const missingKey = config && !settings.llmKey && !config.server_has_llm_key;
  const missingToken = config && !settings.parserKey && !config.server_has_parser_token;

  const pick = async (f: File | undefined) => {
    setError(null);
    setPages(null);
    if (!f) return;
    if (f.type && f.type !== "application/pdf") {
      setError("只支持 PDF 文件");
      return;
    }
    if (config && f.size > config.max_upload_mb * 1024 * 1024) {
      setError(`文件超过 ${config.max_upload_mb} MB`);
      return;
    }
    setFile(f);
    try {
      const { countPages } = await import("@/shared/pdf/pdf"); // pdf.js 较大，选了文件才加载
      setPages(await countPages(f));
    } catch {
      setError("无法打开文件，请确认是有效的 PDF");
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    void pick(e.dataTransfer.files[0]);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.createJob(file, {
        cloud_consent: consent,
        quality: settings.quality,
        include_references: settings.includeReferences,
      });
      onCreated(job);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="upload">
      <div className="notice" role="note">
        文件将上传至 <strong>MinerU 云端</strong>进行版面解析，翻译文字会发送给你配置的大模型服务。
        请勿上传涉密或未公开的文档。
      </div>

      <label className="dropzone" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
        <input type="file" accept="application/pdf" onChange={(e) => void pick(e.target.files?.[0])} hidden />
        {file ? (
          <span>
            {file.name}
            {pages !== null && ` · ${pages} 页`}
          </span>
        ) : (
          <span>点击选择或拖入英文论文 PDF</span>
        )}
      </label>

      {estimate && (
        <p className="hint">
          预计费用{" "}
          {estimate.cny_low === null
            ? "按你的服务商计费"
            : `¥${estimate.cny_low.toFixed(2)}–${(estimate.cny_high ?? estimate.cny_low).toFixed(2)}`}
          {estimate.peak && "（当前为 DeepSeek 高峰时段）"}，约 {estimate.minutes} 分钟（不含 MinerU 排队）
          · {settings.quality === "careful" ? "精翻" : "默认档"}
        </p>
      )}
      {config && pages !== null && pages > config.max_pages && (
        <p className="error">PDF 共 {pages} 页，超过上限 {config.max_pages} 页</p>
      )}

      {(missingKey || missingToken) && (
        <p className="error">
          {missingKey && "还没有配置翻译服务的 API Key。"}
          {missingToken && "还没有配置 MinerU Token。"}
          <button type="button" className="link" onClick={onNeedSettings}>
            去设置
          </button>
        </p>
      )}

      <label className="inline consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        我已知晓文件会上传到 MinerU 云端解析
      </label>

      {error && <p className="error">{error}</p>}
      <button
        type="button"
        className="primary"
        disabled={!file || pages === null || !consent || busy || Boolean(missingKey || missingToken)}
        onClick={() => void submit()}
      >
        {busy ? "上传中…" : "开始翻译"}
      </button>
    </section>
  );
}
