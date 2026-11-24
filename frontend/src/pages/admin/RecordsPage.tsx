import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { useAdminConsole } from "@/pages/admin/AdminLayout";
import {
  adminApi,
  AdminApiError,
  type SubmissionPage,
  type SubmissionRecord,
  type SubmissionSearch,
} from "@/shared/api/admin";

const dateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const STATUS_NAMES: Record<SubmissionRecord["status"], string> = {
  queued: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STAGE_NAMES: Record<string, string> = {
  parse: "版面解析",
  fuse: "对齐文字",
  segment: "切分段落",
  glossary: "抽取术语",
  translate: "翻译",
  render: "文档生成",
  done: "已完成",
};

const TITLE_SOURCE_NAMES: Record<SubmissionRecord["title_source"], string> = {
  parsed: "PDF 识别",
  filename: "文件名回退",
  unknown: "未识别",
};

const DEVICE_NAMES: Record<string, string> = {
  desktop: "桌面",
  mobile: "手机",
  tablet: "平板",
  bot: "脚本",
  unknown: "未知",
};

const VALID_STATUSES = new Set<SubmissionRecord["status"]>(["queued", "running", "done", "failed", "cancelled"]);
const VALID_CHANNELS = new Set<"byok" | "free">(["byok", "free"]);
const VALID_DEVICES = new Set<"desktop" | "mobile" | "tablet" | "bot" | "unknown">([
  "desktop",
  "mobile",
  "tablet",
  "bot",
  "unknown",
]);

function dateRange(days: number | null): Pick<SubmissionSearch, "date_from" | "date_to"> {
  if (days === null) return { date_from: null, date_to: null };
  const today = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const first = new Date(`${today}T00:00:00Z`);
  first.setUTCDate(first.getUTCDate() - days + 1);
  return { date_from: first.toISOString().slice(0, 10), date_to: today };
}

function defaultSearch(): SubmissionSearch {
  return {
    title_query: "",
    ip: null,
    channel_mode: null,
    client_device: null,
    status: null,
    ...dateRange(7),
    page: 1,
    page_size: 25,
  };
}

function parseInitialSearch(params: URLSearchParams): SubmissionSearch {
  const base = defaultSearch();
  const status = params.get("status");
  if (status && VALID_STATUSES.has(status as SubmissionRecord["status"])) {
    base.status = status as SubmissionRecord["status"];
  }
  const channel = params.get("channel_mode");
  if (channel && VALID_CHANNELS.has(channel as "byok" | "free")) {
    base.channel_mode = channel as "byok" | "free";
  }
  const device = params.get("client_device");
  if (device && VALID_DEVICES.has(device as "desktop" | "mobile" | "tablet" | "bot" | "unknown")) {
    base.client_device = device as "desktop" | "mobile" | "tablet" | "bot" | "unknown";
  }
  return base;
}

function formatClientLabel(record: SubmissionRecord): string {
  const parts: string[] = [];
  if (record.client_device) {
    parts.push(DEVICE_NAMES[record.client_device] ?? record.client_device);
  }
  if (record.client_os) parts.push(record.client_os);
  if (record.client_browser) parts.push(record.client_browser);
  return parts.length > 0 ? parts.join(" · ") : "未知终端";
}

function statusBadgeTone(status: SubmissionRecord["status"]): string {
  if (status === "done") return "ok";
  if (status === "failed") return "err";
  if (status === "running") return "pen";
  if (status === "queued") return "warn";
  return "";
}

function RecordItem({ record }: { record: SubmissionRecord }) {
  const [ipCopyMessage, setIpCopyMessage] = useState("");
  const [titleCopyMessage, setTitleCopyMessage] = useState("");
  const [uaCopyMessage, setUaCopyMessage] = useState("");
  const [titleExpanded, setTitleExpanded] = useState(false);
  const [uaExpanded, setUaExpanded] = useState(false);

  const title = record.title || "未记录标题";
  const clientLabel = formatClientLabel(record);

  async function copyIp() {
    if (!record.submitter_ip) return;
    try {
      await navigator.clipboard.writeText(record.submitter_ip);
      setIpCopyMessage("已复制");
    } catch {
      setIpCopyMessage("复制失败，请手动选择完整 IP。");
    }
  }

  async function copyTitle() {
    try {
      await navigator.clipboard.writeText(title);
      setTitleCopyMessage("已复制");
    } catch {
      setTitleCopyMessage("复制失败");
    }
  }

  async function copyUa() {
    if (!record.user_agent) return;
    try {
      await navigator.clipboard.writeText(record.user_agent);
      setUaCopyMessage("已复制");
    } catch {
      setUaCopyMessage("复制失败");
    }
  }

  return (
    <li>
      <article className="ac-record-card admin-record">
        <div className="ac-record-grid">
          <div>
            <p className="ac-record-title" title={title}>
              {title}
            </p>
            <div className="ac-record-meta">
              <time dateTime={record.submitted_at}>{dateTime.format(new Date(record.submitted_at))}</time>
              <span>{record.record_source === "backfill" ? "历史补录" : "实时记录"}</span>
              <span>{TITLE_SOURCE_NAMES[record.title_source]}</span>
              <span>{record.page_count} 页</span>
              <button
                type="button"
                className="ac-link-btn"
                aria-expanded={titleExpanded}
                onClick={() => setTitleExpanded((prev) => !prev)}
              >
                {titleExpanded ? "收起标题" : "查看完整标题"}
              </button>
            </div>
            {titleExpanded && (
              <div className="ac-title-full">
                <span>{title}</span>
                <div className="ac-actions">
                  <button type="button" className="ac-link-btn" onClick={() => void copyTitle()}>
                    复制标题
                  </button>
                  {titleCopyMessage && (
                    <span className="ac-copy-msg" role="status">
                      {titleCopyMessage}
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>

          <div className="ac-ip-box">
            <div className="ac-ip-line">
              {record.submitter_ip ? (
                <>
                  <code className="admin-ip">{record.submitter_ip}</code>
                  <button
                    type="button"
                    className="ac-link-btn"
                    aria-label="复制提交 IP"
                    onClick={() => void copyIp()}
                  >
                    复制
                  </button>
                </>
              ) : (
                <span className="ac-hint">未记录</span>
              )}
            </div>
            {ipCopyMessage && (
              <span className="ac-copy-msg" role="status">
                {ipCopyMessage}
              </span>
            )}
            <div className="ac-ua-line">
              <span className={`ac-badge${record.client_device === "bot" ? " warn" : ""}`}>{clientLabel}</span>
              {record.user_agent && (
                <button
                  type="button"
                  className="ac-link-btn"
                  aria-expanded={uaExpanded}
                  onClick={() => setUaExpanded((prev) => !prev)}
                >
                  {uaExpanded ? "收起 UA" : "完整 UA"}
                </button>
              )}
            </div>
          </div>

          <div className="ac-record-right">
            <div className="ac-actions" style={{ justifyContent: "flex-end" }}>
              <span className={`ac-badge ${statusBadgeTone(record.status)}`}>{STATUS_NAMES[record.status]}</span>
              {record.channel_mode && (
                <span className={`ac-badge ${record.channel_mode === "free" ? "pen" : ""}`}>
                  {record.channel_mode === "free" ? "免费体验" : "自备 Key"}
                </span>
              )}
            </div>
            {record.model_name && (
              <div className="ac-mono">
                {record.model_name}
                {record.fallback_used && <span className="ac-badge warn" style={{ marginLeft: 6 }}>备选</span>}
              </div>
            )}
            {(record.current_stage || record.status === "running" || record.status === "queued") && (
              <span>当前阶段：{STAGE_NAMES[record.current_stage ?? ""] ?? record.current_stage ?? "等待调度"}</span>
            )}
            <span>
              首次完成：{record.first_completed_at ? dateTime.format(new Date(record.first_completed_at)) : "未记录"}
            </span>
            {record.failure_updated_at && (
              <span>失败更新时间：{dateTime.format(new Date(record.failure_updated_at))}</span>
            )}
          </div>
        </div>

        {uaExpanded && record.user_agent && (
          <div className="ac-ua-raw">
            <span>{record.user_agent}</span>
            <div className="ac-actions" style={{ flexShrink: 0 }}>
              <button type="button" className="ac-link-btn" onClick={() => void copyUa()}>
                复制 UA
              </button>
              {uaCopyMessage && (
                <span className="ac-copy-msg" role="status">
                  {uaCopyMessage}
                </span>
              )}
            </div>
          </div>
        )}

        {record.status === "failed" && (
          <div className="ac-record-fail">
            <span>
              失败原因：{record.failure_reason || "该记录创建时未保存失败原因"}
              {record.failure_code ? ` · 失败类型：${record.failure_code}` : ""}
            </span>
            {record.retryable !== null && record.retryable !== undefined && (
              <strong>{record.retryable ? "可以重试" : "暂不建议重试"}</strong>
            )}
          </div>
        )}
      </article>
    </li>
  );
}

export function RecordsPage() {
  const { csrfToken, submissionAuditEnabled, handleUnauthorized, refreshSession } = useAdminConsole();
  const [searchParams] = useSearchParams();
  const initialQuery = parseInitialSearch(searchParams);

  const [draft, setDraft] = useState<SubmissionSearch>(initialQuery);
  const [applied, setApplied] = useState<SubmissionSearch>(initialQuery);
  const [data, setData] = useState<SubmissionPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [csrfBroken, setCsrfBroken] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});

  const controllerRef = useRef<AbortController | null>(null);
  const queryRef = useRef<SubmissionSearch>(initialQuery);
  const successfulQueryRef = useRef<SubmissionSearch>(initialQuery);

  const runSearch = useCallback(
    async (next: SubmissionSearch, overrideToken?: string, syncDraft = false) => {
      const token = overrideToken ?? csrfToken;
      if (!token) return;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      queryRef.current = next;
      if (syncDraft) setDraft(next);
      setBusy(true);
      setError("");
      setCsrfBroken(false);
      setFields({});
      try {
        const result = await adminApi.search(next, token, controller.signal);
        if (controller.signal.aborted) return;
        successfulQueryRef.current = next;
        setApplied(next);
        setData(result);
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        if (reason instanceof AdminApiError && reason.status === 403) {
          setCsrfBroken(true);
          setError("请求验证未通过，请重新验证管理会话。");
        } else if (reason instanceof AdminApiError && reason.status === 422) {
          setFields(reason.fields);
          setError("查询条件无效，请检查标题、完整 IP 和起止日期。");
        } else if (reason instanceof AdminApiError) {
          setError(reason.message);
        } else {
          setError("请求失败，请检查网络后重试。");
        }
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [csrfToken, handleUnauthorized],
  );

  // 首次挂载或路由查询参数变化时查询记录（不含切窗监听）。
  const paramSignature = searchParams.toString();
  useEffect(() => {
    if (!csrfToken) return;
    const next = parseInitialSearch(new URLSearchParams(paramSignature));
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    queryRef.current = next;
    void adminApi.search(next, csrfToken, controller.signal).then(
      (result) => {
        if (controller.signal.aborted) return;
        successfulQueryRef.current = next;
        setDraft(next);
        setApplied(next);
        setData(result);
        setError("");
        setCsrfBroken(false);
        setFields({});
        setBusy(false);
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        if (reason instanceof AdminApiError && reason.status === 403) {
          setCsrfBroken(true);
          setError("请求验证未通过，请重新验证管理会话。");
        } else if (reason instanceof AdminApiError && reason.status === 422) {
          setFields(reason.fields);
          setError("查询条件无效，请检查标题、完整 IP 和起止日期。");
        } else if (reason instanceof AdminApiError) {
          setError(reason.message);
        } else {
          setError("请求失败，请检查网络后重试。");
        }
        setBusy(false);
      },
    );
    return () => controller.abort();
  }, [csrfToken, handleUnauthorized, paramSignature]);

  function submitSearch(next: SubmissionSearch) {
    if (next.date_from && next.date_to && next.date_from > next.date_to) {
      setFields({ date_to: "结束日期不能早于起始日期。" });
      setError("请检查起止日期。");
      return;
    }
    const normalized: SubmissionSearch = {
      ...next,
      title_query: next.title_query.trim(),
      ip: next.ip?.trim() || null,
    };
    setDraft(normalized);
    void runSearch(normalized);
  }

  function handleFormSubmit(event: FormEvent) {
    event.preventDefault();
    submitSearch({ ...draft, page: 1 });
  }

  async function handleReverify() {
    const refreshed = await refreshSession();
    if (refreshed) {
      void runSearch(queryRef.current, refreshed.csrf_token);
    }
  }

  return (
    <>
      {!submissionAuditEnabled && (
        <div className="ac-alert info" role="status">
          <span>
            当前站点未开启提交明细采集（仅展示已有历史记录或空列表）；运行概况、服务设置与系统设置不受影响。
          </span>
        </div>
      )}

      <section className="ac-card" aria-labelledby="records-heading">
        <div className="ac-card-hd">
          <div>
            <h2 className="ac-card-title" id="records-heading">
              翻译记录查询
            </h2>
            <p className="ac-card-sub">
              使用 POST 加密通道检索非正文元数据，完整展示提交 IP 与客户端 UA；日期均为北京时间。
            </p>
          </div>
          <div className="ac-actions">
            <span className="ac-badge mono" role="status">
              {busy ? "正在查询…" : data ? `符合条件 ${data.total} 条 · 第 ${data.page} 页` : "尚未取得查询结果"}
            </span>
            <button
              type="button"
              className="ac-btn sm"
              disabled={busy}
              onClick={() => submitSearch({ ...successfulQueryRef.current, page: 1 })}
            >
              刷新记录
            </button>
          </div>
        </div>

        <form autoComplete="off" onSubmit={handleFormSubmit}>
          <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="visually-hidden">筛选翻译记录</legend>

            <div className="ac-filter-grid">
              <div className="ac-field">
                <label htmlFor="admin-title">标题关键词</label>
                <input
                  id="admin-title"
                  type="text"
                  maxLength={100}
                  placeholder="论文标题关键词"
                  value={draft.title_query}
                  onChange={(event) => setDraft({ ...draft, title_query: event.target.value })}
                  aria-invalid={!!fields.title_query}
                  aria-describedby={fields.title_query ? "admin-title-error" : undefined}
                />
                {fields.title_query && (
                  <span className="ac-field-err" id="admin-title-error">
                    {fields.title_query}
                  </span>
                )}
              </div>

              <div className="ac-field">
                <label htmlFor="admin-ip">完整提交 IP</label>
                <input
                  id="admin-ip"
                  type="text"
                  maxLength={64}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="完整 IPv4 / IPv6"
                  value={draft.ip ?? ""}
                  onChange={(event) => setDraft({ ...draft, ip: event.target.value })}
                  aria-invalid={!!fields.ip}
                  aria-describedby={fields.ip ? "admin-ip-error" : undefined}
                />
                {fields.ip && (
                  <span className="ac-field-err" id="admin-ip-error">
                    {fields.ip}
                  </span>
                )}
              </div>

              <div className="ac-field">
                <label htmlFor="admin-channel">通道方式</label>
                <select
                  id="admin-channel"
                  value={draft.channel_mode ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      channel_mode: (event.target.value || null) as SubmissionSearch["channel_mode"],
                    })
                  }
                >
                  <option value="">全部方式</option>
                  <option value="free">免费体验</option>
                  <option value="byok">自备 Key</option>
                </select>
              </div>

              <div className="ac-field">
                <label htmlFor="admin-device">终端类型</label>
                <select
                  id="admin-device"
                  value={draft.client_device ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      client_device: (event.target.value || null) as SubmissionSearch["client_device"],
                    })
                  }
                >
                  <option value="">全部终端</option>
                  <option value="desktop">桌面</option>
                  <option value="mobile">手机</option>
                  <option value="tablet">平板</option>
                  <option value="bot">脚本</option>
                </select>
              </div>

              <div className="ac-field">
                <label htmlFor="admin-status">任务状态</label>
                <select
                  id="admin-status"
                  value={draft.status ?? ""}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      status: (event.target.value || null) as SubmissionSearch["status"],
                    })
                  }
                >
                  <option value="">全部状态</option>
                  <option value="queued">排队中</option>
                  <option value="running">处理中</option>
                  <option value="done">已完成</option>
                  <option value="failed">失败</option>
                  <option value="cancelled">已取消</option>
                </select>
              </div>
            </div>

            <div className="ac-filter-dates">
              <div className="ac-field">
                <label htmlFor="admin-date-from">起始日期</label>
                <input
                  id="admin-date-from"
                  type="date"
                  value={draft.date_from ?? ""}
                  onChange={(event) => setDraft({ ...draft, date_from: event.target.value || null })}
                  aria-invalid={!!fields.date_from}
                  aria-describedby={fields.date_from ? "admin-date-from-error" : undefined}
                />
                {fields.date_from && (
                  <span className="ac-field-err" id="admin-date-from-error">
                    {fields.date_from}
                  </span>
                )}
              </div>

              <div className="ac-field">
                <label htmlFor="admin-date-to">结束日期</label>
                <input
                  id="admin-date-to"
                  type="date"
                  value={draft.date_to ?? ""}
                  onChange={(event) => setDraft({ ...draft, date_to: event.target.value || null })}
                  aria-invalid={!!fields.date_to}
                  aria-describedby={fields.date_to ? "admin-date-to-error" : undefined}
                />
                {fields.date_to && (
                  <span className="ac-field-err" id="admin-date-to-error">
                    {fields.date_to}
                  </span>
                )}
              </div>

              <div className="ac-shortcuts">
                <div className="ac-actions" aria-label="快捷日期范围">
                  {([7, 30, null] as const).map((days) => (
                    <button
                      key={days ?? "all"}
                      type="button"
                      className="ac-chip"
                      disabled={busy}
                      onClick={() => submitSearch({ ...draft, ...dateRange(days), page: 1 })}
                    >
                      {days === null ? "全部" : `近 ${days} 天`}
                    </button>
                  ))}
                </div>
                <div className="ac-actions">
                  <button type="button" className="ac-btn sm" onClick={() => submitSearch(defaultSearch())}>
                    重置
                  </button>
                  <button type="submit" className="ac-btn primary sm">
                    查询
                  </button>
                </div>
              </div>
            </div>
          </fieldset>
        </form>

        {error && (
          <div className="ac-alert err" role="alert" style={{ marginBottom: 12 }}>
            <span>
              {error}
              {data ? " 当前保留上一次查询结果。" : ""}
            </span>
            <div className="ac-actions">
              {csrfBroken && (
                <button type="button" className="ac-btn sm" onClick={() => void handleReverify()}>
                  重新验证会话
                </button>
              )}
              {!data && !csrfBroken && (
                <button type="button" className="ac-btn sm" disabled={busy} onClick={() => void runSearch(queryRef.current)}>
                  重试查询
                </button>
              )}
            </div>
          </div>
        )}

        {data && (
          <>
            <p className="ac-hint" style={{ marginTop: 0, marginBottom: 10 }}>
              更新于 {dateTime.format(new Date(data.generated_at))}。正文与 PDF 文件按保留期自动清理，新提交可能改变翻页位置。
            </p>

            {data.items.length > 0 ? (
              <ol className="ac-records-list" aria-label="翻译记录列表">
                {data.items.map((record) => (
                  <RecordItem key={record.job_id} record={record} />
                ))}
              </ol>
            ) : (
              <p className="ac-empty" role="status">
                {data.total > 0
                  ? "本页暂无记录，请刷新回到第一页。"
                  : applied.title_query || applied.ip || applied.status || applied.channel_mode || applied.client_device
                    ? "没有符合筛选条件的翻译记录。"
                    : "该时段暂无提交记录，可调整日期范围。"}
              </p>
            )}

            <nav className="ac-pagination" aria-label="翻译记录分页">
              <label htmlFor="admin-page-size" className="ac-actions">
                <span>每页</span>
                <select
                  id="admin-page-size"
                  value={data.page_size}
                  disabled={busy}
                  onChange={(event) =>
                    submitSearch({
                      ...successfulQueryRef.current,
                      page: 1,
                      page_size: Number(event.target.value) as SubmissionSearch["page_size"],
                    })
                  }
                >
                  {[25, 50, 100].map((size) => (
                    <option key={size} value={size}>
                      {size} 条
                    </option>
                  ))}
                </select>
              </label>
              <span>
                第 {data.page} 页 / 共 {Math.max(1, Math.ceil(data.total / data.page_size))} 页
              </span>
              <button
                type="button"
                className="ac-btn sm"
                disabled={busy || data.page <= 1}
                onClick={() => submitSearch({ ...successfulQueryRef.current, page: data.page - 1 })}
              >
                上一页
              </button>
              <button
                type="button"
                className="ac-btn sm"
                disabled={busy || data.page * data.page_size >= data.total}
                onClick={() => submitSearch({ ...successfulQueryRef.current, page: data.page + 1 })}
              >
                下一页
              </button>
            </nav>
          </>
        )}
      </section>
    </>
  );
}
