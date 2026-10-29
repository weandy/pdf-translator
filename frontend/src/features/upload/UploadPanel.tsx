import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link } from "react-router";

import { addHistory, findByHash, type HistoryEntry } from "@/entities/history/model";
import { rememberJob } from "@/entities/job/model";
import { termMap } from "@/entities/glossary/model";
import { getSettings, saveSettings, useSettings } from "@/entities/settings/model";
import { parsePageRange } from "@/features/upload/pages";
import { api, type Job } from "@/shared/api/client";
import { fingerprint } from "@/shared/file/fingerprint";

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
  hash: string | null; // 文件指纹，只在本机用
  // 本机历史里翻译过同一文件：alive 表示服务器上的译文还在（24 小时内）
  seen: { entry: HistoryEntry; alive: boolean } | null;
  confirmed: boolean; // 翻译过的文件，用户点了“仍要重新翻译”
}

const when = (iso: string) =>
  new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

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
  const [rangeText, setRangeText] = useState("");
  const consent = settings.cloudConsent; // 勾一次就记在浏览器里，以后默认勾上
  const setConsent = (v: boolean) => saveSettings({ ...getSettings(), cloudConsent: v });
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const usable = items.filter((it) => !it.error);
  const undecided = usable.filter((it) => it.seen && !it.confirmed); // 翻译过、还没确认要不要重翻
  const counted = usable.every((it) => it.pages !== null);
  const file = items.length === 1 ? items[0] : null;
  // 只选一个文件时可以只翻部分页（版面仍解析全文，翻译与费用按所选页数）
  const range = file && file.pages !== null && !file.error ? parsePageRange(rangeText, file.pages) : null;
  const rangeError = range && !Array.isArray(range) ? range.error : null;
  const selected = Array.isArray(range) ? range : null;
  const pages = items.length && counted ? (selected?.length ?? usable.reduce((n, it) => n + (it.pages ?? 0), 0)) : null;

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: pages !== null && pages > 0,
  });

  const actions = useRef<HTMLDivElement>(null);
  const ready = items.length > 0 && items.every((it) => it.pages !== null || it.error);
  const shown = ready && Boolean(estimate);
  useEffect(() => {
    // 选好文件后，小屏上“开始翻译”可能在屏幕外：费用预估出来（会把按钮往下推）后滚到能看见的位置
    if (shown) actions.current?.scrollIntoView({ block: "nearest" });
  }, [shown]);

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
    setRangeText("");
    setItems(
      chosen.map((f) => ({
        file: f,
        pages: null,
        error: check(f),
        state: "ready",
        hash: null,
        seen: null,
        confirmed: false,
      })),
    );
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
          // 与本机历史比对（指纹不发给服务器）；翻译过的问一下服务器上那次的任务还在不在
          const hash = await fingerprint(f);
          const prior = hash ? findByHash(hash)[0] : undefined;
          const alive = prior
            ? await api.job(prior.id).then(
                () => true,
                () => false,
              )
            : false;
          update(f, { hash, seen: prior ? { entry: prior, alive } : null });
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
      ...(items.length === 1 && selected ? { pages: selected } : {}),
    };
    const created: Job[] = [];
    // 最多同时上传 PARALLEL_UPLOADS 个；某个失败（如服务器排队已满）只标在那个文件上
    const worker = async () => {
      for (let it = queue.shift(); it; it = queue.shift()) {
        update(it.file, { state: "uploading" });
        try {
          const job = await api.createJob(it.file, options);
          rememberJob(job.id);
          if (it.hash) {
            addHistory({ id: job.id, hash: it.hash, filename: job.filename, pages: job.page_count,
                         createdAt: job.created_at }); // prettier-ignore
          }
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
                {it.seen && !it.error && it.state === "ready" && (
                  <small className="seen-tag"> · {when(it.seen.entry.createdAt)} 翻译过</small>
                )}
              </span>
              {it.seen && !it.confirmed && it.state === "ready" && !it.error && !busy && (
                <button type="button" className="quiet" onClick={() => update(it.file, { confirmed: true })}>
                  仍要翻译
                </button>
              )}
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
      {items.length > 1 && undecided.length > 0 && (
        <p className="seen-notice">
          有 {undecided.length} 篇你在这个浏览器里翻译过，请先对每篇选择“仍要翻译”或“移除”。
        </p>
      )}
      {file?.seen && !file.confirmed && !file.error && (
        <div className="seen-notice" role="status">
          <p>
            这篇你在 {when(file.seen.entry.createdAt)} 翻译过（{file.seen.entry.filename}）。
            {file.seen.alive
              ? "服务器上的译文还在，可以直接打开。"
              : "服务器上的译文已过期删除，可以先在下载目录里找找当时下载的 PDF（文件名一般以“-译文”或“-双语对照”结尾）。"}
          </p>
          <div className="row-actions">
            {file.seen.alive && (
              <Link className="button primary" to={`/jobs/${file.seen.entry.id}`}>
                打开上次的译文
              </Link>
            )}
            <button
              type="button"
              className={file.seen.alive ? "quiet" : ""}
              onClick={() => update(file.file, { confirmed: true })}
            >
              仍要重新翻译
            </button>
          </div>
        </div>
      )}
      {file && file.pages !== null && !file.error && (
        <label className="field page-range">
          <span>页码范围（可选）</span>
          <input
            value={rangeText}
            onChange={(e) => setRangeText(e.target.value)}
            placeholder={`如 1-10, 15；留空翻译全部 ${file.pages} 页`}
            aria-invalid={Boolean(rangeError)}
          />
          {rangeError ? (
            <small className="error">{rangeError}</small>
          ) : selected ? (
            <small className="muted">只翻译这 {selected.length} 页，其余页保持原文</small>
          ) : null}
        </label>
      )}
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
          config.retention_hours > 0 &&
          `原文与译文在服务器上只保留 ${config.retention_hours} 小时，之后自动删除，请及时下载。`}
        版面解析在高峰时可能要排队 10 分钟以上。
      </p>

      <label className="consent">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        我知道文件会上传到 {service} 云端解析
      </label>

      {error && <p className="error">{error}</p>}
      <ol className="how-to" aria-label="使用步骤">
        <li>选择英文论文 PDF（可以一次选多个）</li>
        <li>等版面解析和翻译，一般 1–4 分钟，可以先离开</li>
        <li>对照阅读，点段落修改，下载中文版或双语 PDF</li>
        <li className="how-to-more">
          <Link to="/guide">完整使用指南</Link>
        </li>
      </ol>
      <div className="upload-actions" ref={actions}>
        <button
          type="button"
          className="primary"
          disabled={
            !usable.length ||
            !counted ||
            !consent ||
            busy ||
            undecided.length > 0 ||
            Boolean(rangeError) ||
            Boolean(missingKey || missingToken)
          }
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
