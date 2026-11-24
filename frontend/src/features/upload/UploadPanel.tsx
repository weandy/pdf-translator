import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link } from "react-router";

import { addHistory, findByHash, type HistoryEntry } from "@/entities/history/model";
import { rememberJob } from "@/entities/job/model";
import { termMap } from "@/entities/glossary/model";
import {
  confirmLlmKeyBinding,
  confirmParserKeyBinding,
  getSettings,
  llmKeyNeedsRebind,
  parserKeyNeedsRebind,
  resolveTranslationMode,
  saveSettings,
  uploadAuthHeaders,
  useSettings,
  type TranslationMode,
} from "@/entities/settings/model";
import { parsePageRange } from "@/features/upload/pages";
import { api, type Job, type JobOptions } from "@/shared/api/client";
import { fingerprint } from "@/shared/file/fingerprint";

interface Props {
  onCreated: (job: Job) => void; // 只上传一个文件时：进入这个任务
  onBatchCreated: (jobs: Job[]) => void; // 一次上传多个文件时：留在首页看批次
  onNeedSettings: () => void;
}

const MAX_BATCH = 10; // 一次最多选几个文件
const PARALLEL_UPLOADS = 4; // 同时上传几个
const MAX_INSPECT_CONCURRENCY = 2; // 限制同时 arrayBuffer 计页的文件数，避免多选大 PDF 触发浏览器内存尖峰

async function runWithConcurrency<T>(
  items: readonly T[],
  limit: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const item = items[cursor++]!;
      await worker(item);
    }
  });
  await Promise.all(runners);
}

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
  const {
    data: config,
    isPending: configLoading,
    isError: configError,
    refetch: refetchConfig,
  } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });
  const [items, setItems] = useState<Item[]>([]);
  const [rangeText, setRangeText] = useState("");
  const [userSelectedParser, setUserSelectedParser] = useState<JobOptions["parser"] | null>(null);
  const consent = settings.cloudConsent; // 勾一次就记在浏览器里，以后默认勾上
  const setConsent = (v: boolean) => saveSettings({ ...getSettings(), cloudConsent: v });
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const configWaiting = !config && !configError && configLoading;
  const configFailed = !config && configError;
  const maintenance = Boolean(config?.maintenance_mode);
  const siteNotice = config?.site_notice?.trim() ?? "";
  const freeChannel = config?.free_channel;
  const freeEnabled = Boolean(freeChannel?.enabled);
  const mode = resolveTranslationMode(settings, config);
  const isFree = freeEnabled && mode === "free";
  const selectMode = (next: TranslationMode) => saveSettings({ ...getSettings(), translationMode: next });
  const paddleAvailable = Boolean(config?.paddle_public_enabled && config?.server_has_paddle_token);
  const mineruAvailable = isFree
    ? Boolean(config?.server_has_parser_token)
    : Boolean(config?.server_has_parser_token || settings.parserKey.trim());
  const effectiveParser: JobOptions["parser"] = !paddleAvailable
    ? "mineru-cloud"
    : isFree && !mineruAvailable
      ? "paddleocr-vl"
      : (userSelectedParser ?? (!mineruAvailable ? "paddleocr-vl" : "mineru-cloud"));
  const isPaddle = effectiveParser === "paddleocr-vl";
  const service = isPaddle ? "PaddleOCR" : "MinerU";

  const freeLimit =
    isFree && freeChannel?.max_pages != null
      ? Math.min(config?.max_pages ?? freeChannel.max_pages, freeChannel.max_pages)
      : null;
  const freeLimitError = (it: Item): string | null =>
    freeLimit !== null && it.pages !== null && !it.error && it.pages > freeLimit
      ? `共 ${it.pages} 页，超过免费体验上限 ${freeLimit} 页`
      : null;
  const itemError = (it: Item): string | null => it.error ?? freeLimitError(it);

  const usable = items.filter((it) => !itemError(it));
  const overFreeLimit = items.filter((it) => Boolean(freeLimitError(it)));
  const undecided = usable.filter((it) => it.seen && !it.confirmed); // 翻译过、还没确认要不要重翻
  const counted = usable.every((it) => it.pages !== null);
  const file = items.length === 1 ? items[0] : null;
  const fileError = file ? itemError(file) : null;
  // 只选一个文件时可以只翻部分页（版面仍解析全文，翻译与费用按所选页数）
  const range = file && file.pages !== null && !fileError ? parsePageRange(rangeText, file.pages) : null;
  const rangeError = range && !Array.isArray(range) ? range.error : null;
  const selected = Array.isArray(range) ? range : null;
  const pages = items.length && counted ? (selected?.length ?? usable.reduce((n, it) => n + (it.pages ?? 0), 0)) : null;

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: Boolean(config) && !isFree && pages !== null && pages > 0,
  });

  const actions = useRef<HTMLDivElement>(null);
  const pickSeqRef = useRef(0);
  useEffect(
    () => () => {
      // 组件卸载时作废未完成的计页与指纹检查，避免离开页面后继续读取剩余大文件
      pickSeqRef.current += 1;
    },
    [],
  );
  const ready = items.length > 0 && items.every((it) => it.pages !== null || it.error);
  const shown = ready && (isFree || Boolean(estimate));
  useEffect(() => {
    // 选好文件后，小屏上“开始翻译”可能在屏幕外：费用预估出来（会把按钮往下推）后滚到能看见的位置
    if (shown) actions.current?.scrollIntoView({ block: "nearest" });
  }, [shown]);

  const missingKey = !isFree && Boolean(config && !settings.llmKey && !config.server_has_llm_key);
  const missingToken = isPaddle ? false : Boolean(config && !mineruAvailable);
  const keyNeedsRebind = !isFree && Boolean(config) && llmKeyNeedsRebind(settings, config);
  const parserNeedsRebind = !isFree && !isPaddle && Boolean(config) && parserKeyNeedsRebind(settings, config);
  const freeClosedWarning = Boolean(config && !freeEnabled && settings.translationMode === "free");
  const update = (f: File, patch: Partial<Item>) =>
    setItems((list) => list.map((it) => (it.file === f ? { ...it, ...patch } : it)));

  const pick = async (picked: File[]) => {
    setError(null);
    if (!picked.length) return;
    const seq = ++pickSeqRef.current;
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
    if (seq !== pickSeqRef.current) return;
    await runWithConcurrency(
      chosen.filter((f) => !check(f)),
      MAX_INSPECT_CONCURRENCY,
      async (f) => {
        if (seq !== pickSeqRef.current) return;
        try {
          const n = await countPages(f);
          if (seq !== pickSeqRef.current) return;
          const over = config && n > config.max_pages ? `共 ${n} 页，超过上限 ${config.max_pages} 页` : null;
          update(f, { pages: n, error: over });
        } catch {
          if (seq !== pickSeqRef.current) return;
          update(f, { error: "无法打开，请确认是有效的 PDF" });
        }
        // 与本机历史比对（指纹不发给服务器）；翻译过的问一下服务器上那次的任务还在不在
        const hash = await fingerprint(f);
        if (seq !== pickSeqRef.current) return;
        const prior = hash ? findByHash(hash)[0] : undefined;
        const alive = prior
          ? await api.job(prior.id).then(
              () => true,
              () => false,
            )
          : false;
        if (seq !== pickSeqRef.current) return;
        update(f, { hash, seen: prior ? { entry: prior, alive } : null });
      },
    );
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void pick([...e.dataTransfer.files]);
  };

  const submit = async () => {
    if (!config || maintenance) return;
    if (!isFree && llmKeyNeedsRebind(getSettings(), config)) return;
    if (!isFree && !isPaddle && parserKeyNeedsRebind(getSettings(), config)) return;
    const queue = items.filter((it) => !itemError(it) && it.state === "ready");
    if (!queue.length) return;
    setBusy(true);
    setError(null);
    const options: Partial<JobOptions> = {
      cloud_consent: consent,
      user_terms: termMap(),
      quality: settings.quality,
      include_references: settings.includeReferences,
      ...(isPaddle ? { parser: "paddleocr-vl" as const } : {}),
      ...(items.length === 1 && selected ? { pages: selected } : {}),
    };
    const submitMode: TranslationMode = isFree ? "free" : "byok";
    const submitHeaders = uploadAuthHeaders(submitMode, getSettings());
    const created: Job[] = [];
    // 最多同时上传 PARALLEL_UPLOADS 个；某个失败（如服务器排队已满）只标在那个文件上
    const worker = async () => {
      for (let it = queue.shift(); it; it = queue.shift()) {
        update(it.file, { state: "uploading" });
        try {
          const job = await api.createJob(it.file, options, { mode: submitMode, headers: submitHeaders });
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
      onBatchCreated(created);
      setItems((list) => list.filter((it) => it.state !== "done")); // 留下没传成功的，方便重试
    }
  };
  const uploading = items.filter((it) => it.state !== "ready").length;

  const cost =
    !isFree &&
    estimate &&
    (estimate.cny_low === null
      ? "按你的服务商计费"
      : `¥${estimate.cny_low.toFixed(2)}–${(estimate.cny_high ?? estimate.cny_low).toFixed(2)}`);

  return (
    <section className="upload" aria-label="上传论文">
      {configWaiting && (
        <p className="hint" role="status">
          正在加载站点配置…
        </p>
      )}
      {configFailed && (
        <div className="site-banner maintenance" role="alert">
          无法获取站点配置，为避免误用计费通道已暂停提交。
          <button type="button" className="link" onClick={() => void refetchConfig()}>
            重试加载配置
          </button>
        </div>
      )}
      {maintenance ? (
        <div className="site-banner maintenance" role="alert">
          {siteNotice || "系统正在维护，暂不接收新任务；已完成的任务仍可继续阅读与下载。"}
        </div>
      ) : siteNotice ? (
        <div className="site-banner info" role="status">
          {siteNotice}
        </div>
      ) : null}
      {freeClosedWarning && (
        <div className="site-banner info" role="status">
          {settings.llmKey
            ? "免费体验通道当前未开放，如需继续翻译将使用你已配置的自备 API Key 按实际用量计费。"
            : "免费体验通道当前未开放，如需继续翻译请使用自备 API Key。"}
        </div>
      )}

      {freeEnabled && (
        <div className="mode-panel">
          <div className="segmented mode-switch" role="group" aria-label="翻译通道">
            <button
              type="button"
              className={isFree ? "on" : undefined}
              aria-pressed={isFree}
              onClick={() => selectMode("free")}
            >
              免费体验（免填 API Key）
            </button>
            <button
              type="button"
              className={!isFree ? "on" : undefined}
              aria-pressed={!isFree}
              onClick={() => selectMode("byok")}
            >
              自备 API Key
            </button>
          </div>
          {isFree ? (
            <div className="mode-card free" role="status">
              <div className="mode-card-head">
                {freeChannel?.model_display && (
                  <span>
                    当前免费模型：<strong>{freeChannel.model_display}</strong>
                  </span>
                )}
                {freeLimit !== null && <span className="num">单篇上限 {freeLimit} 页</span>}
              </div>
              <p className="hint">
                {freeChannel?.notice?.trim() ||
                  `经 ${service} 云端解析版面，并通过站点公共推理接口翻译，仅建议用于公开论文体验。`}
              </p>
            </div>
          ) : (
            <div className="mode-card byok">
              <p className="hint">
                翻译队列独立，支持自定义服务商与模型。
                <button type="button" className="link" onClick={onNeedSettings}>
                  设置服务商与 Key
                </button>
              </p>
            </div>
          )}
        </div>
      )}

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
                {fileError ?? (file.pages === null ? "正在读取页数…" : `${file.pages} 页，点击可换一个文件`)}
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

      {!isFree && estimate && (
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
          {items.map((it) => {
            const err = itemError(it);
            return (
              <li key={`${it.file.name}:${it.file.size}:${it.file.lastModified}`}>
                <span className="file-name" title={it.file.name}>
                  {it.file.name}
                </span>
                <span className={err ? "error" : "muted num"}>
                  {err ??
                    (it.state === "uploading"
                      ? "上传中…"
                      : it.state === "done"
                        ? "已加入"
                        : it.pages === null
                          ? "读取中…"
                          : `${it.pages} 页`)}
                  {it.seen && !err && it.state === "ready" && (
                    <small className="seen-tag"> · {when(it.seen.entry.createdAt)} 翻译过</small>
                  )}
                </span>
                {it.seen && !it.confirmed && it.state === "ready" && !err && !busy && (
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
            );
          })}
        </ul>
      )}
      {overFreeLimit.length > 0 && freeLimit !== null && (
        <p className="free-limit-notice" role="status">
          当前免费体验单篇上限为 {freeLimit} 页，超出上限的论文可改用自备 API Key 翻译。
          <button type="button" className="link" onClick={() => selectMode("byok")}>
            切换到自备 API Key
          </button>
        </p>
      )}
      {items.length > 1 && <p className="hint">上传后可在下方“我的任务”查看进度，每篇翻译完成后即可下载。</p>}
      {items.length > 1 && undecided.length > 0 && (
        <p className="seen-notice">
          有 {undecided.length} 篇你在这个浏览器里翻译过，请先对每篇选择“仍要翻译”或“移除”。
        </p>
      )}
      {file?.seen && !file.confirmed && !fileError && (
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
      {file && file.pages !== null && !fileError && (
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
      {file && fileError && (
        <p className="error">
          {file.file.name}：{fileError}
        </p>
      )}

      {keyNeedsRebind && config?.default_base_url && (
        <div className="key-rebind-notice" role="alert">
          <p>
            当前个人 API Key 尚未绑定到新默认服务地址（目标：{config.default_base_url}），请确认目标地址或重新填写。
          </p>
          <div className="row-actions">
            <button type="button" onClick={() => confirmLlmKeyBinding(config)}>
              确认发送到此地址
            </button>
            <button type="button" className="quiet" onClick={onNeedSettings}>
              打开设置更换 Key
            </button>
          </div>
        </div>
      )}

      {parserNeedsRebind && config?.default_mineru_base_url && (
        <div className="key-rebind-notice" role="alert">
          <p>
            当前个人 MinerU Token 尚未绑定到当前解析服务地址（目标：{config.default_mineru_base_url}），请确认目标地址或更换 Token。
          </p>
          <div className="row-actions">
            <button type="button" onClick={() => saveSettings(confirmParserKeyBinding(getSettings(), config))}>
              确认发送到此解析地址
            </button>
            <button type="button" className="quiet" onClick={onNeedSettings}>
              打开设置更换 Token
            </button>
          </div>
        </div>
      )}

      {(missingKey || missingToken) && (
        <p className="setup-hint">
          开始前，先在设置里填写
          {missingKey && missingToken
            ? `翻译服务的 API Key 和 ${service} Token`
            : missingKey
              ? "翻译服务的 API Key"
              : `${service} Token`}
          。
          {missingToken
            ? "MinerU Token 在 MinerU API 管理中申请。"
            : isPaddle
              ? "本站已提供 PaddleOCR 版面解析。"
              : settings.parserKey
                ? "版面解析将使用你自己的 MinerU Token。"
                : config?.server_has_parser_token
                  ? "本站已提供 MinerU 版面解析。"
                  : null}
          {missingKey && "翻译费用由你选择的服务商收取。"}
          <button type="button" className="link" onClick={onNeedSettings}>
            打开设置
          </button>
        </p>
      )}

      {paddleAvailable && (
        <div className="parser-choice">
          <span className="hint">版面解析服务</span>
          <div className="segmented" role="group" aria-label="版面解析服务">
            {(!isFree || mineruAvailable) && (
              <button
                type="button"
                className={!isPaddle ? "on" : undefined}
                aria-pressed={!isPaddle}
                onClick={() => setUserSelectedParser("mineru-cloud")}
              >
                MinerU
              </button>
            )}
            <button
              type="button"
              className={isPaddle ? "on" : undefined}
              aria-pressed={isPaddle}
              onClick={() => setUserSelectedParser("paddleocr-vl")}
            >
              PaddleOCR-VL
            </button>
          </div>
        </div>
      )}

      <p className="notice">
        {isFree
          ? `文件会上传到 ${service} 云端解析版面，正文会发往本站提供的免费公共推理接口翻译，请仅上传公开论文，不要上传涉密或未公开的文档。`
          : `文件会上传到 ${service} 云端解析版面，正文会发给你设置的翻译服务，请不要上传涉密或未公开的文档。`}
        {config &&
          config.retention_hours > 0 &&
          `原文与译文在服务器上只保留 ${config.retention_hours} 小时，之后自动删除，请及时下载。`}
        版面解析在高峰时可能要排队 10 分钟以上。
      </p>
      {config?.submission_audit_enabled && (
        <p className="notice" role="note">
          本服务已开启提交审计，会按数据保留说明留存任务摘要、提交 IP 与客户端 User-Agent（浏览器/系统标识）等必要信息用于排障与审计（免费体验与自备 Key 模式均适用），公开统计仅展示聚合数量；正文和文件按保留期清理，正在运行的任务可能延后。
          <a href="/guide#privacy">查看数据保留说明</a>
        </p>
      )}

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
            !config ||
            !usable.length ||
            !counted ||
            !consent ||
            busy ||
            maintenance ||
            undecided.length > 0 ||
            Boolean(rangeError) ||
            Boolean(missingKey || missingToken || keyNeedsRebind || parserNeedsRebind)
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
