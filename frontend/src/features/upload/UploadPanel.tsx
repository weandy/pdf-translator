import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type DragEvent, type SetStateAction } from "react";
import { Link } from "react-router";

import { addHistory, findByHash, type HistoryEntry } from "@/entities/history/model";
import { rememberJob } from "@/entities/job/model";
import { termMap } from "@/entities/glossary/model";
import {
  confirmLlmKeyBinding,
  confirmParserKeyBinding,
  effectiveNormalizedLlmTarget,
  getSettings,
  llmKeyNeedsRebind,
  parserKeyNeedsRebind,
  resolveTranslationMode,
  saveSettings,
  savePrefs,
  uploadAuthHeaders,
  useSettings,
  type TranslationMode,
} from "@/entities/settings/model";
import { parsePageRange } from "@/features/upload/pages";
import { api, type Job, type JobOptions } from "@/shared/api/client";
import { readCachedConfig, writeCachedConfig } from "@/shared/api/configCache";
import { useToast } from "@/shared/feedback/Toast";
import { fingerprint } from "@/shared/file/fingerprint";

import { allFilesChecked, confirmPendingDuplicates, isPendingDuplicate, removePendingDuplicates, uploadQueue } from "./batchDedup";

interface Props {
  onCreated: (job: Job) => void; // 只上传一个文件时：进入这个任务
  onBatchCreated: (jobs: Job[]) => void; // 一次上传多个文件时：留在首页看批次
  onNeedSettings: () => void;
  onOpenTerms?: () => void;
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
  checking: boolean;
  confirmed: boolean; // 翻译过的文件，用户点了“仍要重新翻译”
}

const checkingLabel = ({ pages }: Item) => pages === null ? "检查中…" : `${pages} 页 · 检查历史…`;

const when = (iso: string) =>
  new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function UploadPanel({ onCreated, onBatchCreated, onNeedSettings, onOpenTerms }: Props) {
  const settings = useSettings();
  const toast = useToast();
  const {
    data: config,
    isPending: configLoading,
    isError: configError,
    isPlaceholderData: configPlaceholder,
    refetch: refetchConfig,
  } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
    placeholderData: readCachedConfig,
  });
  const confirmedConfig = config && !configPlaceholder ? config : undefined;
  useEffect(() => {
    if (confirmedConfig) writeCachedConfig(confirmedConfig);
  }, [confirmedConfig]);
  const [{ items, rangeText, pickSeq }, setSelection] = useState({ items: [] as Item[], rangeText: "", pickSeq: 0 });
  const setItems = (update: SetStateAction<Item[]>) =>
    setSelection((previous) => ({ ...previous, items: typeof update === "function" ? update(previous.items) : update }));
  const setRangeText = (rangeText: string) => setSelection((previous) => ({ ...previous, rangeText }));
  const [userSelectedParser, setUserSelectedParser] = useState<JobOptions["parser"] | null>(null);
  const consent = settings.cloudConsent; // 勾一次就记在浏览器里，以后默认勾上
  const setConsent = (v: boolean) => reportStorage(savePrefs({ cloudConsent: v }));
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const configWaiting = !config && !configError && configLoading;
  const configFailed = !confirmedConfig && configError;
  const maintenance = Boolean(config?.maintenance_mode);
  const siteNotice = config?.site_notice?.trim() ?? "";
  const freeChannel = config?.free_channel;
  const freeEnabled = Boolean(freeChannel?.enabled);
  const mode = resolveTranslationMode(settings, config);
  const isFree = freeEnabled && mode === "free";
  const selectMode = (next: TranslationMode) => reportStorage(savePrefs({ translationMode: next }));
  const reportStorage = (result: ReturnType<typeof saveSettings>) => {
    if (!result.ok) toast.error(result.error);
  };
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
  const undecided = usable.filter((it) => isPendingDuplicate(it, itemError));
  const allChecked = allFilesChecked(usable);
  const counted = usable.every((it) => it.pages !== null);
  const file = items.length === 1 ? items[0] : null;
  const fileError = file ? itemError(file) : null;
  // 只选一个文件时开放页码范围。
  const range = file && file.pages !== null && !fileError ? parsePageRange(rangeText, file.pages) : null;
  const rangeError = range && !Array.isArray(range) ? range.error : null;
  const selected = Array.isArray(range) ? range : null;
  const pages = items.length && counted ? (selected?.length ?? usable.reduce((n, it) => n + (it.pages ?? 0), 0)) : null;

  const { data: estimate } = useQuery({
    queryKey: ["estimate", pages, settings.quality, settings.model],
    queryFn: () => api.estimate(pages ?? 1, settings.quality, settings.model || undefined),
    enabled: Boolean(confirmedConfig) && !isFree && pages !== null && pages > 0,
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

  const missingKey = !isFree && Boolean(confirmedConfig && !settings.llmKey && !confirmedConfig.server_has_llm_key);
  const missingToken = isPaddle ? false : Boolean(confirmedConfig && !mineruAvailable);
  const keyNeedsRebind = !isFree && Boolean(confirmedConfig) && llmKeyNeedsRebind(settings, confirmedConfig);
  const parserNeedsRebind = !isFree && !isPaddle && Boolean(confirmedConfig) && parserKeyNeedsRebind(settings, confirmedConfig);
  const freeClosedWarning = Boolean(confirmedConfig && !freeEnabled && settings.translationMode === "free");
  const update = (f: File, patch: Partial<Item>) =>
    setItems((list) => list.map((it) => (it.file === f ? { ...it, ...patch } : it)));

  const pick = async (picked: File[]) => {
    if (busy || submitting.current || !picked.length) return;
    setError(null);
    const seq = ++pickSeqRef.current;
    if (picked.length > MAX_BATCH) setError(`一次最多上传 ${MAX_BATCH} 个文件，只取前 ${MAX_BATCH} 个`);
    const chosen = picked.slice(0, MAX_BATCH);
    const check = (f: File) =>
      f.type && f.type !== "application/pdf"
        ? "只支持 PDF 文件"
        : config && f.size > config.max_upload_mb * 1024 * 1024
          ? `超过 ${config.max_upload_mb} MB`
          : null;
    const candidates = chosen.map((file): Item => {
      const error = check(file);
      return { file, error, pages: null, state: "ready", hash: null, seen: null, confirmed: false, checking: !error };
    });
    setSelection({ items: candidates, rangeText: "", pickSeq: seq });
    const pdf = import("@/shared/pdf/pdf"); // pdf.js 较大，选了文件才加载
    await runWithConcurrency(
      candidates.filter((item) => item.checking),
      MAX_INSPECT_CONCURRENCY,
      async ({ file: f }) => {
        if (seq !== pickSeqRef.current) return;
        try {
          const { countPages } = await pdf;
          if (seq !== pickSeqRef.current) return;
          const n = await countPages(f);
          if (seq !== pickSeqRef.current) return;
          const over = config && n > config.max_pages ? `共 ${n} 页，超过上限 ${config.max_pages} 页` : null;
          update(f, { pages: n, error: over });
          if (over) return;
          // 指纹只用于查询本机历史，服务端只按此前的任务编号查询。
          const hash = await fingerprint(f);
          if (seq !== pickSeqRef.current) return;
          const prior = hash ? findByHash(hash)[0] : undefined;
          const alive = prior ? await api.job(prior.id).then(() => true, () => false) : false;
          if (seq === pickSeqRef.current) update(f, { hash, seen: prior ? { entry: prior, alive } : null });
        } catch {
          if (seq === pickSeqRef.current) update(f, { error: "无法检查文件，请确认是有效的 PDF" });
        } finally {
          if (seq === pickSeqRef.current) update(f, { checking: false });
        }
      },
    );
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (busy || submitting.current) return;
    void pick([...e.dataTransfer.files]);
  };

  const submit = async () => {
    if (submitting.current || busy || !confirmedConfig || maintenance || pickSeq !== pickSeqRef.current) return;
    if (!allChecked || undecided.length || rangeError || !consent || missingKey || missingToken) return;
    if (!isFree && llmKeyNeedsRebind(getSettings(), confirmedConfig)) return;
    if (!isFree && !isPaddle && parserKeyNeedsRebind(getSettings(), confirmedConfig)) return;
    const queue = uploadQueue(items, itemError);
    if (!queue.length) return;
    const options: Partial<JobOptions> = {
      cloud_consent: consent,
      user_terms: termMap(),
      quality: settings.quality,
      include_references: settings.includeReferences,
      ...(isPaddle ? { parser: "paddleocr-vl" as const } : {}),
      ...(items.length === 1 && selected ? { pages: selected } : {}),
    };
    const submitMode: TranslationMode = isFree ? "free" : "byok";
    const submitHeaders = uploadAuthHeaders(submitMode, getSettings(), confirmedConfig);
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
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await Promise.all(Array.from({ length: PARALLEL_UPLOADS }, worker));
      if (items.length === 1 && created[0]) onCreated(created[0]);
      else if (created.length) {
        onBatchCreated(created);
        setItems((list) => list.filter((it) => it.state !== "done"));
      }
    } finally {
      submitting.current = false;
      setBusy(false);
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
    <section className="upload upload-card" aria-label="上传论文">
      {configWaiting && (
        <div className="mode-panel mode-panel-skeleton" role="status" aria-live="polite">
          <div className="mode-switch" aria-hidden="true">
            <div className="mode-tab mode-tab-skeleton" />
            <div className="mode-tab mode-tab-skeleton" />
          </div>
          <div className="mode-summary mode-summary-skeleton">
            <span>正在加载站点配置…</span>
          </div>
        </div>
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
          <div className="mode-switch" role="group" aria-label="翻译通道">
            <button
              type="button"
              className={`mode-tab${isFree ? " on" : ""}`}
              aria-pressed={isFree}
              onClick={() => selectMode("free")}
            >
              <span className="mode-tab-title">免费体验</span>
              <span className="mode-tab-sub">免填 API Key · 即传即翻</span>
            </button>
            <button
              type="button"
              className={`mode-tab${!isFree ? " on" : ""}`}
              aria-pressed={!isFree}
              onClick={() => selectMode("byok")}
            >
              <span className="mode-tab-title">自备 API Key</span>
              <span className="mode-tab-sub">独立队列 · 自定义模型</span>
            </button>
          </div>
          {isFree ? (
            <div className="mode-summary free" role="status">
              <div className="mode-summary-main">
                {freeChannel?.model_display && (
                  <span>
                    当前模型：<strong>{freeChannel.model_display}</strong>
                  </span>
                )}
                {freeLimit !== null && <span className="mode-pill num">上限 {freeLimit} 页</span>}
              </div>
              {freeChannel?.notice?.trim() && <span className="mode-summary-note">{freeChannel.notice.trim()}</span>}
            </div>
          ) : (
            <div className="mode-summary byok">
              <span>使用你配置的翻译服务与独立队列</span>
              <button type="button" className="link" onClick={onNeedSettings}>
                设置服务商与 Key
              </button>
            </div>
          )}
        </div>
      )}

      <label
        className={`dropzone${dragging ? " dragging" : ""}${items.length > 0 ? " has-file" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(!busy && !submitting.current);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input
          type="file"
          disabled={busy}
          accept="application/pdf"
          multiple
          className="visually-hidden"
          onChange={(e) => {
            void pick([...(e.target.files ?? [])]);
            e.target.value = ""; // 允许再次选择同一个文件
          }}
        />
        <span className="dropzone-icon" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <path d="M12 18v-6" />
            <path d="m9.5 14.5 2.5-2.5 2.5 2.5" />
          </svg>
        </span>
        <span className="dropzone-label">
          {file ? (
            <>
              <span className="file-name">{file.file.name}</span>
              <span className="dropzone-sub">
                {fileError ?? (file.checking ? checkingLabel(file) : `${file.pages} 页 · 点击可更换文件`)}
              </span>
            </>
          ) : items.length > 1 ? (
            <>
              <span className="file-name">已选 {items.length} 个 PDF 文件</span>
              <span className="dropzone-sub">
                {pages === null ? "正在读取页数…" : `共 ${pages} 页 · 点击可重新选择`}
              </span>
            </>
          ) : (
            <>
              <strong>选择英文论文 PDF</strong>
              <span className="dropzone-sub">拖拽 PDF 到此处，或点击浏览（支持一次多选）</span>
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
                        : it.checking ? checkingLabel(it) : `${it.pages} 页`)}
                  {it.seen && !err && it.state === "ready" && (
                    <small className="seen-tag"> · {when(it.seen.entry.createdAt)} 翻译过</small>
                  )}
                </span>
                {isPendingDuplicate(it, itemError) && !busy && (
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
      {items.length > 1 && undecided.length > 0 && !busy && (
        <div className="seen-notice">
          <p>有 {undecided.length} 篇你在这个浏览器里翻译过，请确认是否重新翻译。</p>
          <div className="row-actions">
            <button type="button" onClick={() => setItems((list) => confirmPendingDuplicates(list, itemError))}>
              全部仍要翻译
            </button>
            <button type="button" className="quiet" onClick={() => setSelection((value) => removePendingDuplicates(value, itemError))}>
              移除待确认项
            </button>
          </div>
        </div>
      )}
      {file?.seen && isPendingDuplicate(file, itemError) && !busy && (
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
            <small className="muted">
              只翻译这 {selected.length} 页，其余页保持原文
              {!isPaddle && "；MinerU 将尽量仅解析所需页（小文档或含封面时可能附带解析更多页）"}
            </small>
          ) : null}
        </label>
      )}
      {file && fileError && (
        <p className="error">
          {file.file.name}：{fileError}
        </p>
      )}

      {keyNeedsRebind && config && (
        <div className="key-rebind-notice" role="alert">
          <p>
            当前个人 API Key 尚未绑定到当前服务地址，请检查目标（{effectiveNormalizedLlmTarget(settings, config)?.url ?? "地址无效"}）或重新填写。
          </p>
          <div className="row-actions">
            <button type="button" disabled={!effectiveNormalizedLlmTarget(settings, config)} onClick={() => { if (settings === getSettings()) reportStorage(confirmLlmKeyBinding(config)); }}>
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
            当前个人 MinerU Token 尚未绑定到当前解析服务地址（目标：{config.default_mineru_base_url}
            ），请确认目标地址或更换 Token。
          </p>
          <div className="row-actions">
            <button type="button" onClick={() => { if (settings === getSettings()) reportStorage(saveSettings(confirmParserKeyBinding(settings, config))); }}>
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

      <div className="consent-block">
        <label className="consent">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>
            我知道文件会上传到 {service} 云端解析，并同意
            {onOpenTerms ? (
              <button type="button" className="link" onClick={onOpenTerms}>
                《服务条款与隐私说明》
              </button>
            ) : (
              <Link to="/tos">《服务条款与隐私说明》</Link>
            )}
          </span>
        </label>
        {config?.submission_audit_enabled && (
          <p className="consent-audit-note" role="note">
            已开启提交审计（仅记录标题、提交 IP 与 User-Agent 等必要信息，公开统计仅展示聚合数量）·{" "}
            <a href="/guide#privacy">隐私说明</a>
          </p>
        )}
      </div>

      {error && <p className="error">{error}</p>}
      <div className="upload-actions" ref={actions}>
        <button
          type="button"
          className="primary"
          disabled={
            !confirmedConfig ||
            !usable.length ||
            !allChecked ||
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
              ? isFree
                ? `开始免费翻译 ${usable.length} 篇`
                : `开始翻译 ${usable.length} 篇`
              : isFree
                ? "开始免费翻译"
                : "开始翻译"}
        </button>
      </div>
    </section>
  );
}
