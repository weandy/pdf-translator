import { useQuery } from "@tanstack/react-query";
import { useState, type DragEvent } from "react";

import { rememberJob } from "@/entities/job/model";
import { useSettings } from "@/entities/settings/model";
import { api, type Job } from "@/shared/api/client";

interface Props {
  onCreated: (job: Job) => void;
  onNeedSettings: () => void;
}

/** 对开页里的版面示意：标题、正文、公式。左页英文、右页中文（中文行用蓝铅笔色）。 */
function SketchPage({ lang }: { lang: "en" | "zh" }) {
  return (
    <div className={`spread-page ${lang}`} aria-hidden="true">
      <i className="ln title" />
      <i className="ln" />
      <i className="ln" />
      <i className="ln short" />
      <i className="ln eq" />
      <i className="ln" />
      <i className="ln" />
      <i className="ln" />
      <i className="ln short" />
      <i className="fig" />
      <i className="ln" />
      <i className="ln short" />
    </div>
  );
}

export function UploadPanel({ onCreated, onNeedSettings }: Props) {
  const settings = useSettings();
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const [file, setFile] = useState<File | null>(null);
  const [pages, setPages] = useState<number | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: pages !== null,
  });

  const missingKey = config && !settings.llmKey && !config.server_has_llm_key;
  const missingToken = config && !settings.parserKey && !config.server_has_parser_token;
  const tooManyPages = config && pages !== null && pages > config.max_pages;

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
    setDragging(false);
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
        translate_figures: settings.translateFigures,
      });
      rememberJob(job.id);
      onCreated(job);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cost =
    estimate &&
    (estimate.cny_low === null
      ? "按你的服务商计费"
      : `¥${estimate.cny_low.toFixed(2)}–${(estimate.cny_high ?? estimate.cny_low).toFixed(2)}`);

  return (
    <section className="upload" aria-label="上传论文">
      <label
        className={`spread${dragging ? " dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          type="file"
          accept="application/pdf"
          className="visually-hidden"
          onChange={(e) => void pick(e.target.files?.[0])}
        />
        <SketchPage lang="en" />
        <SketchPage lang="zh" />
        <span className="spread-label">
          {file ? (
            <>
              <span className="file-name">{file.name}</span>
              <span>{pages === null ? "正在读取页数…" : `${pages} 页，点击可换一个文件`}</span>
            </>
          ) : (
            <>
              <strong>选择英文论文 PDF</strong>
              <span>或拖到这里</span>
            </>
          )}
        </span>
      </label>

      {estimate && (
        <dl className="upload-meta">
          <div>
            <dt>预计费用</dt>
            <dd className="num">{cost}</dd>
          </div>
          <div>
            <dt>翻译用时</dt>
            <dd className="num">约 {estimate.minutes} 分钟</dd>
          </div>
          <div>
            <dt>档位</dt>
            <dd>{settings.quality === "careful" ? "精翻" : "默认"}</dd>
          </div>
          {estimate.peak && <p className="hint">现在是 DeepSeek 高峰时段，价格翻倍。</p>}
        </dl>
      )}
      {tooManyPages && (
        <p className="error">
          这份 PDF 共 {pages} 页，超过上限 {config.max_pages} 页。
        </p>
      )}

      {(missingKey || missingToken) && (
        <p className="setup-hint">
          开始前，先在设置里填写
          {missingKey && missingToken
            ? "翻译服务的 API Key 和 MinerU Token"
            : missingKey
              ? "翻译服务的 API Key"
              : "MinerU Token"}
          ，两者都可以免费申请。
          <button type="button" className="link" onClick={onNeedSettings}>
            打开设置
          </button>
        </p>
      )}

      <p className="notice">
        文件会上传到 MinerU 云端解析版面，正文会发给你设置的翻译服务，请不要上传涉密或未公开的文档。
        {config &&
          config.retention_days > 0 &&
          `文件在本站保留 ${config.retention_days} 天后自动删除，也可以随时手动删除。`}
        版面解析在高峰时可能要排队 10 分钟以上。
      </p>

      <label className="consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        我知道文件会上传到 MinerU 云端解析
      </label>

      {error && <p className="error">{error}</p>}
      <div className="upload-actions">
        <button
          type="button"
          className="primary"
          disabled={!file || pages === null || !consent || busy || tooManyPages || Boolean(missingKey || missingToken)}
          onClick={() => void submit()}
        >
          {busy ? "正在上传…" : "开始翻译"}
        </button>
      </div>
    </section>
  );
}
