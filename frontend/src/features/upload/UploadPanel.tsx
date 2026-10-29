import { useQuery } from "@tanstack/react-query";
import { useState, type DragEvent } from "react";

import { rememberJob } from "@/entities/job/model";
import { termMap } from "@/entities/glossary/model";
import { useSettings } from "@/entities/settings/model";
import { api, type Job } from "@/shared/api/client";

interface Props {
  onCreated: (job: Job) => void; // 只上传一个文件时：进入这个任务
  onBatchCreated: (count: number) => void; // 一次上传多个文件时：留在首页看列表
  onNeedSettings: () => void;
}

const MAX_BATCH = 10; // 一次最多选几个文件
const PARALLEL_UPLOADS = 4; // 同时上传几个

interface Item {
  file: File;
  pages: number | null;
  error: string | null;
  state: "ready" | "uploading" | "done";
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

export function UploadPanel({ onCreated, onBatchCreated, onNeedSettings }: Props) {
  const settings = useSettings();
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const [items, setItems] = useState<Item[]>([]);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const usable = items.filter((it) => !it.error);
  const counted = usable.every((it) => it.pages !== null);
  const pages = items.length && counted ? usable.reduce((n, it) => n + (it.pages ?? 0), 0) : null;
  const file = items.length === 1 ? items[0] : null;

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: pages !== null && pages > 0,
  });

  const missingKey = config && !settings.llmKey && !config.server_has_llm_key;
  const service = "MinerU";
  const missingToken = config && !settings.parserKey && !config.server_has_parser_token;
  const update = (f: File, patch: Partial<Item>) =>
    setItems((list) => list.map((it) => (it.file === f ? { ...it, ...patch } : it)));

  const pick = async (picked: File[]) => {
    setError(null);
    if (!picked.length) return;
    if (picked.length > MAX_BATCH) setError(`一次最多上传 ${MAX_BATCH} 个文件，只取前 ${MAX_BATCH} 个`);
    const chosen = picked.slice(0, MAX_BATCH);
    const check = (f: File) =>
      f.type && f.type !== "application/pdf"
        ? "只支持 PDF 文件"
        : config && f.size > config.max_upload_mb * 1024 * 1024
          ? `超过 ${config.max_upload_mb} MB`
          : null;
    setItems(chosen.map((f) => ({ file: f, pages: null, error: check(f), state: "ready" })));
    const { countPages } = await import("@/shared/pdf/pdf"); // pdf.js 较大，选了文件才加载
    await Promise.all(
      chosen
        .filter((f) => !check(f))
        .map(async (f) => {
          try {
            const n = await countPages(f);
            const over = config && n > config.max_pages ? `共 ${n} 页，超过上限 ${config.max_pages} 页` : null;
            update(f, { pages: n, error: over });
          } catch {
            update(f, { error: "无法打开，请确认是有效的 PDF" });
          }
        }),
    );
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void pick([...e.dataTransfer.files]);
  };

  const submit = async () => {
    const queue = items.filter((it) => !it.error && it.state === "ready");
    if (!queue.length) return;
    setBusy(true);
    setError(null);
    const options = {
      cloud_consent: consent,
      user_terms: termMap(),
      quality: settings.quality,
      include_references: settings.includeReferences,
    };
    const created: Job[] = [];
    // 最多同时上传 PARALLEL_UPLOADS 个；某个失败（如服务器排队已满）只标在那个文件上
    const worker = async () => {
      for (let it = queue.shift(); it; it = queue.shift()) {
        update(it.file, { state: "uploading" });
        try {
          const job = await api.createJob(it.file, options);
          rememberJob(job.id);
          created.push(job);
          update(it.file, { state: "done" });
        } catch (e) {
          update(it.file, { state: "ready", error: e instanceof Error ? e.message : String(e) });
        }
      }
    };
    await Promise.all(Array.from({ length: PARALLEL_UPLOADS }, worker));
    setBusy(false);
    if (items.length === 1 && created[0]) onCreated(created[0]);
    else if (created.length) {
      onBatchCreated(created.length);
      setItems((list) => list.filter((it) => it.state !== "done")); // 留下没传成功的，方便重试
    }
  };
  const uploading = items.filter((it) => it.state !== "ready").length;

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
          multiple
          className="visually-hidden"
          onChange={(e) => {
            void pick([...(e.target.files ?? [])]);
            e.target.value = ""; // 允许再次选择同一个文件
          }}
        />
        <SketchPage lang="en" />
        <SketchPage lang="zh" />
        <span className="spread-label">
          {file ? (
            <>
              <span className="file-name">{file.file.name}</span>
              <span>
                {file.error ?? (file.pages === null ? "正在读取页数…" : `${file.pages} 页，点击可换一个文件`)}
              </span>
            </>
          ) : items.length > 1 ? (
            <>
              <span className="file-name">{items.length} 个文件</span>
              <span>{pages === null ? "正在读取页数…" : `共 ${pages} 页，点击可重新选择`}</span>
            </>
          ) : (
            <>
              <strong>选择英文论文 PDF</strong>
              <span>或拖到这里，一次可以选多个</span>
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
      {items.length > 1 && (
        <ul className="batch-list" aria-label="待上传的文件">
          {items.map((it) => (
            <li key={`${it.file.name}:${it.file.size}:${it.file.lastModified}`}>
              <span className="file-name" title={it.file.name}>
                {it.file.name}
              </span>
              <span className={it.error ? "error" : "muted num"}>
                {it.error ??
                  (it.state === "uploading"
                    ? "上传中…"
                    : it.state === "done"
                      ? "已加入"
                      : it.pages === null
                        ? "读取中…"
                        : `${it.pages} 页`)}
              </span>
              {it.state === "ready" && !busy && (
                <button
                  type="button"
                  className="quiet"
                  aria-label={`移除 ${it.file.name}`}
                  onClick={() => setItems((list) => list.filter((x) => x !== it))}
                >
                  移除
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {items.length > 1 && <p className="hint">几篇同时解析，翻译一篇接一篇进行；完成后在下方“我的任务”里打开。</p>}
      {file?.error && (
        <p className="error">
          {file.file.name}：{file.error}
        </p>
      )}

      {(missingKey || missingToken) && (
        <p className="setup-hint">
          开始前，先在设置里填写
          {missingKey && missingToken
            ? `翻译服务的 API Key 和 ${service} Token`
            : missingKey
              ? "翻译服务的 API Key"
              : `${service} Token`}
          ，两者都可以免费申请。
          <button type="button" className="link" onClick={onNeedSettings}>
            打开设置
          </button>
        </p>
      )}

      <p className="notice">
        文件会上传到 {service} 云端解析版面，正文会发给你设置的翻译服务，请不要上传涉密或未公开的文档。
        {config &&
          config.retention_days > 0 &&
          `文件在本站保留 ${config.retention_days} 天后自动删除，也可以随时手动删除。`}
        版面解析在高峰时可能要排队 10 分钟以上。
      </p>

      <label className="consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        我知道文件会上传到 {service} 云端解析
      </label>

      {error && <p className="error">{error}</p>}
      <div className="upload-actions">
        <button
          type="button"
          className="primary"
          disabled={!usable.length || !counted || !consent || busy || Boolean(missingKey || missingToken)}
          onClick={() => void submit()}
        >
          {busy
            ? `正在上传…${items.length > 1 ? ` ${uploading}/${items.length}` : ""}`
            : usable.length > 1
              ? `开始翻译 ${usable.length} 篇`
              : "开始翻译"}
        </button>
      </div>
    </section>
  );
}
