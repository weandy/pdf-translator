import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import {
  adminApi,
  AdminApiError,
  type AdminSession,
  type SubmissionPage,
  type SubmissionRecord,
  type SubmissionSearch,
} from "@/shared/api/admin";

import "@/pages/admin.css";

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
const statusNames: Record<SubmissionRecord["status"], string> = {
  queued: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

type SubmissionRecordDetails = SubmissionRecord & {
  current_stage?: string | null;
  failure_code?: string | null;
  failure_reason?: string | null;
  retryable?: boolean | null;
  failure_updated_at?: string | null;
};

const stageNames: Record<string, string> = {
  parse: "版面解析",
  translate: "翻译",
  render: "文档生成",
  export: "导出",
};

const titleSourceNames: Record<SubmissionRecord["title_source"], string> = {
  parsed: "PDF 识别",
  filename: "文件名回退",
  unknown: "未识别",
};

function range(days: number | null): Pick<SubmissionSearch, "date_from" | "date_to"> {
  if (days === null) return { date_from: null, date_to: null };
  const today = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const first = new Date(`${today}T00:00:00Z`);
  first.setUTCDate(first.getUTCDate() - days + 1);
  return { date_from: first.toISOString().slice(0, 10), date_to: today };
}

function defaultSearch(): SubmissionSearch {
  return { title_query: "", ip: null, ...range(7), page: 1, page_size: 25 };
}

function requestMessage(error: unknown): string {
  if (error instanceof AdminApiError && error.status === 429) return "操作过于频繁，请稍后重试。";
  if (error instanceof AdminApiError && error.status === 408) return error.message;
  if (error instanceof AdminApiError && error.status === 403) return "请求验证未通过，请重新验证管理会话。";
  return "请求失败，请检查网络后重试。";
}

export interface AdminPageProps {
  adminPath?: string;
}

export function AdminLoginPage({ adminPath = "/weandy" }: AdminPageProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const passwordInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    document.title = "管理登录";
    const current = new AbortController();
    controller.current = current;
    void adminApi.session(current.signal).then(
      () => {
        if (!current.signal.aborted) void navigate(`${adminPath}/submissions`, { replace: true });
      },
      (reason: unknown) => {
        if (current.signal.aborted) return;
        setDisabled(reason instanceof AdminApiError && reason.status === 404);
        setChecking(false);
      },
    );
    const clearPassword = () => {
      if (passwordInput.current) passwordInput.current.value = "";
      setPassword("");
    };
    window.addEventListener("pagehide", clearPassword);
    return () => {
      controller.current?.abort();
      window.removeEventListener("pagehide", clearPassword);
    };
  }, [adminPath, navigate]);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (new TextEncoder().encode(password).length > 256) {
      setError("密码最多 256 个 UTF-8 字节。");
      return;
    }
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setBusy(true);
    setError("");
    try {
      await adminApi.login({ password }, current.signal);
      if (!current.signal.aborted) {
        setPassword("");
        void navigate(`${adminPath}/submissions`, { replace: true });
      }
    } catch (reason) {
      if (current.signal.aborted) return;
      if (reason instanceof AdminApiError && reason.status === 404) setDisabled(true);
      else if (reason instanceof AdminApiError && reason.status === 429) {
        setError("登录尝试过于频繁，请稍后重试。");
      } else setError("管理密码不正确或暂时无法登录。");
    } finally {
      if (!current.signal.aborted) {
        setBusy(false);
        setPassword("");
      }
    }
  }

  return (
    <main className="admin-page admin-login">
      <Link to="/stats">返回公开统计</Link>
      <h1>管理登录</h1>
      <p className="hint">仅供本站管理员查看已留存的提交元数据。</p>
      {checking ? (
        <p role="status">正在验证管理会话…</p>
      ) : disabled ? (
        <p role="status">本站未开启管理功能。</p>
      ) : (
        <form onSubmit={(event) => void login(event)}>
          {location.state?.expired && <p role="status">管理会话已失效，请重新登录。</p>}
          <label htmlFor="admin-password">管理密码</label>
          <div className="admin-password-field">
            <input
              ref={passwordInput}
              id="admin-password"
              name="password"
              type={visible ? "text" : "password"}
              autoComplete="current-password"
              required
              maxLength={256}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
            />
            <button type="button" aria-pressed={visible} onClick={() => setVisible(!visible)}>
              {visible ? "隐藏密码" : "显示密码"}
            </button>
          </div>
          {error && (
            <p role="alert" className="admin-error">
              {error}
            </p>
          )}
          <button type="submit" className="primary" disabled={busy || !password}>
            {busy ? "登录中…" : "登录管理"}
          </button>
        </form>
      )}
    </main>
  );
}

function SubmissionCard({ record }: { record: SubmissionRecord }) {
  const details = record as SubmissionRecordDetails;
  const [copyMessage, setCopyMessage] = useState("");
  const [titleMessage, setTitleMessage] = useState("");
  const [titleExpanded, setTitleExpanded] = useState(false);
  const title = record.title || "未记录标题";
  async function copyIp() {
    try {
      await navigator.clipboard.writeText(record.submitter_ip!);
      setCopyMessage("已复制");
    } catch {
      setCopyMessage("复制失败，请手动选择完整 IP。");
    }
  }
  async function copyTitle() {
    try {
      await navigator.clipboard.writeText(title);
      setTitleMessage("已复制");
    } catch {
      setTitleMessage("复制失败");
    }
  }
  return (
    <li>
      <article className="admin-record">
        <div>
          <span className="admin-cell-label">提交时间</span>
          <time dateTime={record.submitted_at}>{dateTime.format(new Date(record.submitted_at))}</time>
          <small>{record.record_source === "backfill" ? "历史补录" : "实时记录"}</small>
        </div>
        <div className="admin-record-title">
          <span className="admin-cell-label">论文标题</span>
          <p className="admin-title-preview" title={title}>
            {title}
          </p>
          <div className="admin-title-meta">
            <span className="admin-title-source">{titleSourceNames[record.title_source]}</span>
            <button
              type="button"
              className="link admin-title-toggle"
              aria-expanded={titleExpanded}
              onClick={() => setTitleExpanded((expanded) => !expanded)}
            >
              {titleExpanded ? "收起标题" : "查看完整标题"}
            </button>
          </div>
          {titleExpanded && (
            <div className="admin-title-full">
              <span>{title}</span>
              <div className="admin-title-actions">
                <button type="button" className="link" onClick={() => void copyTitle()}>
                  复制标题
                </button>
                <span className="admin-copy-message" role="status">
                  {titleMessage}
                </span>
              </div>
            </div>
          )}
        </div>
        <div>
          <span className="admin-cell-label">提交 IP</span>
          {record.submitter_ip ? (
            <>
              <code className="admin-ip">{record.submitter_ip}</code>
              <button type="button" className="link" onClick={() => void copyIp()} aria-label="复制提交 IP">
                复制
              </button>
              <span className="admin-copy-message" role="status">
                {copyMessage}
              </span>
            </>
          ) : (
            <span>未记录</span>
          )}
        </div>
        <div>
          <span className="admin-cell-label">上传页数</span>
          {record.page_count}
        </div>
        <div>
          <span className="admin-cell-label">任务状态</span>
          <strong>{statusNames[record.status]}</strong>
          {(details.current_stage || record.status === "running" || record.status === "queued") && (
            <small>当前阶段：{stageNames[details.current_stage ?? ""] ?? details.current_stage ?? "等待调度"}</small>
          )}
          {record.status === "failed" && (
            <div className="admin-failure">
              <small>失败原因：{details.failure_reason || "该记录创建时未保存失败原因"}</small>
              {details.failure_code && <small>失败类型：{details.failure_code}</small>}
              {details.retryable !== null && details.retryable !== undefined && (
                <small>{details.retryable ? "可以重试" : "暂不建议重试"}</small>
              )}
            </div>
          )}
          <small>
            首次完成：{record.first_completed_at ? dateTime.format(new Date(record.first_completed_at)) : "未记录"}
          </small>
          {details.failure_updated_at && <small>失败更新时间：{dateTime.format(new Date(details.failure_updated_at))}</small>}
        </div>
      </article>
    </li>
  );
}

export function AdminSubmissionsPage({ adminPath = "/weandy" }: AdminPageProps) {
  const navigate = useNavigate();
  // 旧组件测试仍以 /admin/login 作为登录页；实际 App 传入运行时配置的根路径（如 /weandy）。
  const loginPath = adminPath === "/admin" ? "/admin/login" : adminPath;
  const [draft, setDraft] = useState(defaultSearch);
  const [applied, setApplied] = useState(defaultSearch);
  const [data, setData] = useState<SubmissionPage | null>(null);
  const [view, setView] = useState<"checking" | "ready" | "disabled" | "unavailable" | "logout-error">("checking");
  const [busy, setBusy] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [canExit, setCanExit] = useState(false);
  const [error, setError] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const sensitive = useRef<HTMLDivElement | null>(null);
  const session = useRef<AdminSession | null>(null);
  const logoutToken = useRef<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const expires = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const exiting = useRef(false);
  const query = useRef(draft);
  const successfulQuery = useRef(draft);

  useLayoutEffect(() => {
    // 同步事件可能合并 React 更新；恢复时也明确撤销 pagehide 写入的 hidden 属性。
    if (sensitive.current) sensitive.current.hidden = view !== "ready";
  }, [view, busy, data]);

  const mask = useCallback(() => {
    // pagehide/blur 时同步隐藏 DOM，避免 React 尚未提交更新就被浏览器快照保存。
    if (sensitive.current) sensitive.current.hidden = true;
    if (exiting.current) return;
    generation.current += 1;
    controller.current?.abort();
    clearTimeout(expires.current);
    session.current = null;
    logoutToken.current = null;
    setCanExit(false);
    setView("checking");
    setData(null);
    setError("");
    setFields({});
  }, []);

  const expire = useCallback(() => {
    mask();
    setDraft(defaultSearch());
    setApplied(defaultSearch());
    query.current = defaultSearch();
    successfulQuery.current = defaultSearch();
    void navigate(loginPath, { replace: true, state: { expired: true } });
  }, [loginPath, mask, navigate]);

  const scheduleExpiration = useCallback(
    (deadline: number) => {
      clearTimeout(expires.current);
      const check = () => {
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          expire();
          return;
        }
        // 浏览器 setTimeout 的最大安全延迟约为 24.8 天；30 天会话需要分段重检。
        expires.current = setTimeout(check, Math.min(remaining, 2_147_000_000));
      };
      check();
    },
    [expire],
  );

  const load = useCallback(
    async (next: SubmissionSearch, verify = false) => {
      if (verify) mask();
      else controller.current?.abort();
      const current = new AbortController();
      controller.current = current;
      const version = ++generation.current;
      query.current = next;
      setBusy(true);
      setError("");
      setFields({});
      let authenticated = !verify && session.current !== null;
      try {
        const identity = authenticated ? session.current! : await adminApi.session(current.signal);
        if (current.signal.aborted || version !== generation.current) return;
        if (new Date(identity.expires_at).getTime() <= Date.now()) {
          expire();
          return;
        }
        session.current = identity;
        logoutToken.current = identity.csrf_token;
        authenticated = true;
        setCanExit(true);
        setView("ready");
        scheduleExpiration(new Date(identity.expires_at).getTime());
        const result = await adminApi.search(next, identity.csrf_token, current.signal);
        if (current.signal.aborted || version !== generation.current) return;
        successfulQuery.current = next;
        setApplied(next);
        setData(result);
      } catch (reason) {
        if (current.signal.aborted || version !== generation.current) return;
        if (reason instanceof AdminApiError && reason.status === 401) expire();
        else if (reason instanceof AdminApiError && reason.status === 404) {
          mask();
          setView("disabled");
        } else if (reason instanceof AdminApiError && reason.status === 403) {
          mask();
          setView("unavailable");
          setError(requestMessage(reason));
        } else {
          if (!authenticated) setView("unavailable");
          if (reason instanceof AdminApiError && reason.status === 422) {
            setFields(reason.fields);
            setError("查询条件无效，请检查标题、完整 IP 和起止日期。");
          } else setError(requestMessage(reason));
        }
      } finally {
        if (version === generation.current) setBusy(false);
      }
    },
    [expire, mask, scheduleExpiration],
  );

  useEffect(() => {
    document.title = "提交记录 · 管理";
    const verify = () => {
      if (!exiting.current && document.visibilityState !== "hidden") void load(query.current, true);
    };
    const visibility = () => (document.visibilityState === "hidden" ? mask() : verify());
    void load(query.current, true);
    window.addEventListener("blur", mask);
    window.addEventListener("pagehide", mask);
    window.addEventListener("focus", verify);
    window.addEventListener("pageshow", verify);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      generation.current += 1;
      controller.current?.abort();
      clearTimeout(expires.current);
      window.removeEventListener("blur", mask);
      window.removeEventListener("pagehide", mask);
      window.removeEventListener("focus", verify);
      window.removeEventListener("pageshow", verify);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [load, mask]);

  function search(next: SubmissionSearch) {
    if (next.date_from && next.date_to && next.date_from > next.date_to) {
      setFields({ date_to: "结束日期不能早于起始日期。" });
      setError("请检查起止日期。");
      return;
    }
    const normalized = { ...next, title_query: next.title_query?.trim() ?? "", ip: next.ip?.trim() || null };
    setDraft(normalized);
    void load(normalized);
  }

  async function logout() {
    const token = logoutToken.current;
    if (!token || loggingOut) return;
    mask();
    exiting.current = true;
    setDraft(defaultSearch());
    setApplied(defaultSearch());
    query.current = defaultSearch();
    successfulQuery.current = defaultSearch();
    setLoggingOut(true);
    const current = new AbortController();
    controller.current = current;
    try {
      await adminApi.logout(token, current.signal);
      if (!current.signal.aborted) void navigate(loginPath, { replace: true });
    } catch (reason) {
      if (current.signal.aborted) return;
      if (reason instanceof AdminApiError && reason.status === 401) {
        void navigate(loginPath, { replace: true });
      } else {
        logoutToken.current = token;
        setCanExit(true);
        setView("logout-error");
        setError("已清空本页记录，但未能确认会话已退出，请重试退出管理。");
      }
    } finally {
      if (!current.signal.aborted) setLoggingOut(false);
    }
  }

  return (
    <main className="admin-page">
      <header className="admin-page-head">
        <div>
          <h1>提交记录</h1>
          <p className="hint">已长期留存的提交元数据；正文和文件按保留期清理。日期均为北京时间。</p>
        </div>
        <button type="button" onClick={() => void logout()} disabled={!canExit || loggingOut}>
          {loggingOut ? "正在退出…" : "退出管理"}
        </button>
      </header>
      {view === "checking" && <p role="status">{loggingOut ? "正在撤销管理会话…" : "正在验证管理会话…"}</p>}
      {view === "disabled" && <p role="status">本站未开启管理功能。</p>}
      {(view === "unavailable" || view === "logout-error") && (
        <div className="admin-notice" role="alert">
          <p>{error}</p>
          {view === "unavailable" && (
            <button type="button" onClick={() => void load(query.current, true)}>
              重新验证会话
            </button>
          )}
        </div>
      )}
      <div ref={sensitive} hidden={view !== "ready"} className="admin-sensitive">
        <form
          className="admin-filters"
          autoComplete="off"
          onSubmit={(event) => {
            event.preventDefault();
            search({ ...draft, page: 1 });
          }}
        >
          <fieldset disabled={busy}>
            <legend className="visually-hidden">筛选提交记录</legend>
            <label htmlFor="admin-title">
              标题关键词
              <input
                id="admin-title"
                maxLength={100}
                value={draft.title_query ?? ""}
                onChange={(event) => setDraft({ ...draft, title_query: event.target.value })}
                aria-invalid={!!fields.title_query}
                aria-describedby={fields.title_query ? "admin-title-error" : undefined}
              />
              {fields.title_query && (
                <span className="admin-field-error" id="admin-title-error">
                  {fields.title_query}
                </span>
              )}
            </label>
            <label htmlFor="admin-ip">
              完整提交 IP
              <input
                id="admin-ip"
                value={draft.ip ?? ""}
                maxLength={64}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => setDraft({ ...draft, ip: event.target.value })}
                aria-invalid={!!fields.ip}
                aria-describedby={fields.ip ? "admin-ip-error" : undefined}
              />
              {fields.ip && (
                <span className="admin-field-error" id="admin-ip-error">
                  {fields.ip}
                </span>
              )}
            </label>
            <label htmlFor="admin-date-from">
              起始日期
              <input
                id="admin-date-from"
                type="date"
                value={draft.date_from ?? ""}
                onChange={(event) => setDraft({ ...draft, date_from: event.target.value || null })}
                aria-invalid={!!fields.date_from}
                aria-describedby={fields.date_from ? "admin-date-from-error" : undefined}
              />
              {fields.date_from && (
                <span className="admin-field-error" id="admin-date-from-error">
                  {fields.date_from}
                </span>
              )}
            </label>
            <label htmlFor="admin-date-to">
              结束日期
              <input
                id="admin-date-to"
                type="date"
                value={draft.date_to ?? ""}
                onChange={(event) => setDraft({ ...draft, date_to: event.target.value || null })}
                aria-invalid={!!fields.date_to}
                aria-describedby={fields.date_to ? "admin-date-to-error" : undefined}
              />
              {fields.date_to && (
                <span className="admin-field-error" id="admin-date-to-error">
                  {fields.date_to}
                </span>
              )}
            </label>
            <div className="admin-filter-actions">
              <button type="submit" className="primary">
                查询
              </button>
              <button type="button" onClick={() => search(defaultSearch())}>
                重置
              </button>
            </div>
          </fieldset>
          <div className="admin-shortcuts" aria-label="快捷日期范围">
            {([7, 30, null] as const).map((days) => (
              <button
                key={days ?? "all"}
                type="button"
                disabled={busy}
                onClick={() => search({ ...draft, ...range(days), page: 1 })}
              >
                {days === null ? "全部" : `最近 ${days} 天`}
              </button>
            ))}
          </div>
        </form>
        <div className="admin-results-head">
          <p role="status">
            {busy ? "正在查询…" : data ? `符合条件 ${data.total} 条 · 第 ${data.page} 页` : "尚未取得查询结果"}
          </p>
          <button type="button" disabled={busy} onClick={() => search({ ...successfulQuery.current, page: 1 })}>
            刷新记录
          </button>
        </div>
        {error && (
          <div className="admin-notice" role="alert">
            <p>
              {error}
              {data ? " 当前保留上一次查询结果。" : ""}
            </p>
            {!data && (
              <button type="button" disabled={busy} onClick={() => void load(query.current)}>
                重试查询
              </button>
            )}
          </div>
        )}
        {data && (
          <>
            <p className="hint">
              更新于 {dateTime.format(new Date(data.generated_at))}。新提交可能改变翻页位置，刷新将回到第一页。
            </p>
            <p className="admin-status-help">
              任务状态表示整篇任务的处理进度：排队中等待调度，处理中正在执行，已完成可阅读或导出，失败可查看原因，已取消可重新提交。
            </p>
            {data.items.length ? (
              <>
                <div className="admin-table-head" aria-hidden="true">
                  <span>提交时间</span>
                  <span>论文标题</span>
                  <span>提交 IP</span>
                  <span>上传页数</span>
                  <span>任务状态</span>
                </div>
                <ol className="admin-records" aria-label="提交记录列表">
                  {data.items.map((record) => (
                    <SubmissionCard key={record.job_id} record={record} />
                  ))}
                </ol>
              </>
            ) : (
              <p className="admin-empty" role="status">
                {data.total > 0
                  ? "本页暂无记录，请刷新回到第一页。"
                  : applied.title_query || applied.ip
                    ? "没有符合筛选条件的提交记录。"
                    : "该时段暂无提交记录，可调整日期范围。"}
              </p>
            )}
            <nav className="admin-pagination" aria-label="提交记录分页">
              <label htmlFor="admin-page-size">
                每页
                <select
                  id="admin-page-size"
                  value={data.page_size}
                  disabled={busy}
                  onChange={(event) =>
                    search({
                      ...successfulQuery.current,
                      page: 1,
                      page_size: Number(event.target.value) as NonNullable<SubmissionSearch["page_size"]>,
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
                disabled={busy || data.page <= 1}
                onClick={() => search({ ...successfulQuery.current, page: data.page - 1 })}
              >
                上一页
              </button>
              <button
                type="button"
                disabled={busy || data.page * data.page_size >= data.total}
                onClick={() => search({ ...successfulQuery.current, page: data.page + 1 })}
              >
                下一页
              </button>
            </nav>
          </>
        )}
      </div>
    </main>
  );
}
