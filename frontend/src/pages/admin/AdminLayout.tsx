import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link, useNavigate } from "react-router";

import { adminApi, AdminApiError, type AdminSession } from "@/shared/api/admin";

import "@/pages/admin/admin-console.css";

export type AdminSectionKey = "overview" | "records" | "services" | "settings";

export interface AdminShellSummary {
  revision: number | null;
  secretKeyReady: boolean | null;
  maintenanceMode: boolean;
  warnings: string[];
  failed24h: number;
}

export interface AdminConsoleContextValue {
  adminPath: string;
  csrfToken: string;
  submissionAuditEnabled: boolean;
  handleUnauthorized: () => void;
  refreshSession: () => Promise<AdminSession | null>;
  syncSummary: (patch: Partial<AdminShellSummary>) => void;
}

const AdminConsoleContext = createContext<AdminConsoleContextValue | null>(null);

export function useAdminConsole(): AdminConsoleContextValue {
  const ctx = useContext(AdminConsoleContext);
  if (!ctx) {
    throw new Error("useAdminConsole 必须在 AdminLayout 内部使用");
  }
  return ctx;
}

const SECTION_META: Record<AdminSectionKey, { label: string; title: string; path: string }> = {
  overview: { label: "运行概况", title: "运行概况 · 管理后台", path: "overview" },
  records: { label: "翻译记录", title: "翻译记录 · 管理后台", path: "records" },
  services: { label: "服务设置", title: "服务设置 · 管理后台", path: "services" },
  settings: { label: "系统设置", title: "系统设置 · 管理后台", path: "settings" },
};

function NavIcon({ section }: { section: AdminSectionKey }) {
  if (section === "overview") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <rect x="3" y="3" width="7" height="9" rx="1" />
        <rect x="14" y="3" width="7" height="5" rx="1" />
        <rect x="14" y="12" width="7" height="9" rx="1" />
        <rect x="3" y="16" width="7" height="5" rx="1" />
      </svg>
    );
  }
  if (section === "records") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
        <rect x="9" y="3" width="6" height="4" rx="1" />
        <path d="M9 12h6m-6 4h6" />
      </svg>
    );
  }
  if (section === "services") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48 2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48 2.83-2.83" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M2 14h4m4-6h4m4 8h4" />
    </svg>
  );
}

export interface AdminLayoutProps {
  adminPath: string;
  section: AdminSectionKey;
  submissionAuditEnabled?: boolean;
  children: ReactNode;
}

export function AdminLayout({
  adminPath,
  section,
  submissionAuditEnabled = true,
  children,
}: AdminLayoutProps) {
  const navigate = useNavigate();
  const loginPath = `${adminPath}/login`;
  const [session, setSession] = useState<AdminSession | null>(null);
  const [view, setView] = useState<"checking" | "ready" | "disabled" | "unavailable" | "logout-error">("checking");
  const [error, setError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const [canLogout, setCanLogout] = useState(false);
  const [theme, setTheme] = useState<"system" | "light" | "dark">("system");
  const [summary, setSummary] = useState<AdminShellSummary>({
    revision: null,
    secretKeyReady: null,
    maintenanceMode: false,
    warnings: [],
    failed24h: 0,
  });

  const logoutTokenRef = useRef<string | null>(null);
  const sessionControllerRef = useRef<AbortController | null>(null);
  const expiryTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const summaryLoadedRef = useRef(false);

  const expire = useCallback(() => {
    clearTimeout(expiryTimerRef.current);
    sessionControllerRef.current?.abort();
    logoutTokenRef.current = null;
    setCanLogout(false);
    setSession(null);
    setView("checking");
    void navigate(loginPath, { replace: true, state: { expired: true } });
  }, [loginPath, navigate]);

  const scheduleExpiration = useCallback(
    (deadlineMs: number) => {
      clearTimeout(expiryTimerRef.current);
      const check = () => {
        const remaining = deadlineMs - Date.now();
        if (remaining <= 0) {
          expire();
          return;
        }
        // 浏览器 setTimeout 最大安全延迟约为 24.8 天，长会话按分段重检。
        expiryTimerRef.current = setTimeout(check, Math.min(remaining, 2_147_000_000));
      };
      check();
    },
    [expire],
  );

  const verifySession = useCallback(async (): Promise<AdminSession | null> => {
    sessionControllerRef.current?.abort();
    const controller = new AbortController();
    sessionControllerRef.current = controller;
    setError("");
    try {
      const identity = await adminApi.session(controller.signal);
      if (controller.signal.aborted) return null;
      const deadline = new Date(identity.expires_at).getTime();
      if (Number.isFinite(deadline) && deadline <= Date.now()) {
        expire();
        return null;
      }
      logoutTokenRef.current = identity.csrf_token;
      setCanLogout(true);
      setSession(identity);
      setView("ready");
      if (Number.isFinite(deadline)) {
        scheduleExpiration(deadline);
      }
      return identity;
    } catch (reason) {
      if (controller.signal.aborted) return null;
      if (reason instanceof AdminApiError && reason.status === 401) {
        expire();
      } else if (reason instanceof AdminApiError && reason.status === 404) {
        setSession(null);
        setView("disabled");
      } else {
        setSession(null);
        setView("unavailable");
        setError(reason instanceof AdminApiError ? reason.message : "暂时无法验证管理会话，请检查网络后重试。");
      }
      return null;
    }
  }, [expire, scheduleExpiration]);

  useEffect(() => {
    const controller = new AbortController();
    sessionControllerRef.current?.abort();
    sessionControllerRef.current = controller;
    void adminApi.session(controller.signal).then(
      (identity) => {
        if (controller.signal.aborted) return;
        const deadline = new Date(identity.expires_at).getTime();
        if (Number.isFinite(deadline) && deadline <= Date.now()) {
          expire();
          return;
        }
        logoutTokenRef.current = identity.csrf_token;
        setCanLogout(true);
        setSession(identity);
        setView("ready");
        if (Number.isFinite(deadline)) {
          scheduleExpiration(deadline);
        }
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          expire();
        } else if (reason instanceof AdminApiError && reason.status === 404) {
          setSession(null);
          setView("disabled");
        } else {
          setSession(null);
          setView("unavailable");
          setError(reason instanceof AdminApiError ? reason.message : "暂时无法验证管理会话，请检查网络后重试。");
        }
      },
    );
    return () => {
      clearTimeout(expiryTimerRef.current);
      controller.abort();
    };
  }, [expire, scheduleExpiration]);

  useEffect(() => {
    document.title = SECTION_META[section].title;
  }, [section]);

  // 非运行概况页首次就绪时轻量拉取全局告警与版本号摘要；若失败绝不阻断当前页面。
  useEffect(() => {
    if (view !== "ready" || section === "overview" || summaryLoadedRef.current) return;
    summaryLoadedRef.current = true;
    const controller = new AbortController();
    void adminApi.overview(controller.signal).then(
      (data) => {
        if (controller.signal.aborted) return;
        setSummary({
          revision: data.revision,
          secretKeyReady: data.secret_key_ready,
          maintenanceMode: data.maintenance_mode,
          warnings: data.warnings,
          failed24h: data.stats.failed_24h,
        });
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          expire();
        }
      },
    );
    return () => controller.abort();
  }, [expire, section, view]);

  const syncSummary = useCallback((patch: Partial<AdminShellSummary>) => {
    summaryLoadedRef.current = true;
    setSummary((prev) => ({ ...prev, ...patch }));
  }, []);

  async function handleLogout() {
    const token = logoutTokenRef.current ?? session?.csrf_token;
    if (!token || loggingOut) return;
    clearTimeout(expiryTimerRef.current);
    sessionControllerRef.current?.abort();
    setLoggingOut(true);
    setSession(null);
    setError("");
    const controller = new AbortController();
    sessionControllerRef.current = controller;
    try {
      await adminApi.logout(token, controller.signal);
      if (!controller.signal.aborted) {
        logoutTokenRef.current = null;
        setCanLogout(false);
        void navigate(loginPath, { replace: true });
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (reason instanceof AdminApiError && reason.status === 401) {
        logoutTokenRef.current = null;
        setCanLogout(false);
        void navigate(loginPath, { replace: true });
      } else {
        logoutTokenRef.current = token;
        setCanLogout(true);
        setView("logout-error");
        setError("已清空本页数据，但未能确认会话已退出，请重试退出登录。");
      }
    } finally {
      if (!controller.signal.aborted) setLoggingOut(false);
    }
  }

  const toggleTheme = () => {
    setTheme((current) => {
      if (current === "system") return "dark";
      if (current === "dark") return "light";
      return "dark";
    });
  };

  const contextValue = useMemo<AdminConsoleContextValue>(
    () => ({
      adminPath,
      csrfToken: session?.csrf_token ?? "",
      submissionAuditEnabled,
      handleUnauthorized: expire,
      refreshSession: verifySession,
      syncSummary,
    }),
    [adminPath, expire, session?.csrf_token, submissionAuditEnabled, syncSummary, verifySession],
  );

  const hasAlerts =
    summary.maintenanceMode || summary.secretKeyReady === false || summary.warnings.length > 0;

  return (
    <div className="admin-shell" data-theme={theme === "system" ? undefined : theme}>
      <aside className="admin-side" aria-label="管理导航">
        <div>
          <div className="admin-brand">
            <span className="admin-brand-mark" aria-hidden="true">
              A文
            </span>
            <div>
              <div className="admin-brand-name">PDF 翻译</div>
              <div className="admin-brand-sub">管理后台</div>
            </div>
          </div>

          <nav className="admin-nav" aria-label="一级管理菜单">
            {(["overview", "records", "services", "settings"] as const).map((key) => {
              const meta = SECTION_META[key];
              const isActive = section === key;
              return (
                <Link
                  key={key}
                  to={`${adminPath}/${meta.path}`}
                  className={`admin-nav-link${isActive ? " active" : ""}`}
                  aria-current={isActive ? "page" : undefined}
                >
                  <NavIcon section={key} />
                  <span>{meta.label}</span>
                  {key === "records" && summary.failed24h > 0 && (
                    <span className="admin-nav-badge">{summary.failed24h} 失败</span>
                  )}
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="admin-side-foot">
          {summary.revision !== null && (
            <div className="admin-kv">
              <span>配置版本</span>
              <code>v{summary.revision}</code>
            </div>
          )}
          {summary.secretKeyReady !== null && (
            <div className="admin-kv">
              <span>加密密钥</span>
              <span className={summary.secretKeyReady ? "ac-badge ok" : "ac-badge err"}>
                {summary.secretKeyReady ? "已就绪" : "未就绪"}
              </span>
            </div>
          )}
          <button
            type="button"
            className="ac-btn sm"
            onClick={() => void handleLogout()}
            disabled={loggingOut || !canLogout}
          >
            {loggingOut ? "正在退出…" : "退出登录"}
          </button>
        </div>
      </aside>

      <div className="admin-main">
        <header className="admin-topbar">
          <h1>{SECTION_META[section].label}</h1>
          <div className="admin-topbar-right">
            {hasAlerts && (
              <>
                {summary.maintenanceMode && (
                  <span className="ac-badge warn" role="status">
                    <span className="ac-dot" aria-hidden="true" />
                    维护模式已开启
                  </span>
                )}
                {summary.secretKeyReady === false && (
                  <span className="ac-badge err" role="alert">
                    <span className="ac-dot" aria-hidden="true" />
                    加密主密钥未就绪
                  </span>
                )}
                {summary.warnings.length > 0 && (
                  <span className="ac-badge err" role="alert">
                    <span className="ac-dot" aria-hidden="true" />
                    密文解密告警（{summary.warnings.length}）
                  </span>
                )}
              </>
            )}
            <button type="button" className="ac-btn sm" onClick={toggleTheme} aria-label="切换管理后台深浅主题">
              {theme === "dark" ? "浅色视图" : "深色视图"}
            </button>
            <Link to="/" className="ac-btn sm">
              返回主站
            </Link>
          </div>
        </header>

        <main className="admin-content">
          {view === "checking" && (
            <div className="ac-card" role="status">
              {loggingOut ? "正在撤销管理会话…" : "正在验证管理会话…"}
            </div>
          )}

          {view === "disabled" && (
            <div className="ac-card" role="status">
              本站未开启管理功能。
            </div>
          )}

          {(view === "unavailable" || view === "logout-error") && (
            <div className="ac-alert err" role="alert">
              <span>{error}</span>
              <div className="ac-actions">
                {view === "unavailable" && (
                  <button type="button" className="ac-btn sm" onClick={() => void verifySession()}>
                    重新验证会话
                  </button>
                )}
                {view === "logout-error" && (
                  <button type="button" className="ac-btn sm danger" onClick={() => void handleLogout()}>
                    重试退出登录
                  </button>
                )}
              </div>
            </div>
          )}

          {view === "ready" && session && (
            <AdminConsoleContext.Provider value={contextValue}>{children}</AdminConsoleContext.Provider>
          )}
        </main>
      </div>
    </div>
  );
}
