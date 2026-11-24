import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { adminApi, AdminApiError } from "@/shared/api/admin";

import "@/pages/admin/admin-console.css";

export interface AdminLoginPageProps {
  adminPath?: string;
}

interface LoginLocationState {
  expired?: boolean;
}

export function AdminLoginPage({ adminPath = "/weandy" }: AdminLoginPageProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const locationState = location.state as LoginLocationState | null;
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    document.title = "管理登录 · PDF 保版式翻译";
    const current = new AbortController();
    controller.current = current;
    void adminApi.session(current.signal).then(
      () => {
        if (!current.signal.aborted) {
          void navigate(`${adminPath}/overview`, { replace: true });
        }
      },
      (reason: unknown) => {
        if (current.signal.aborted) return;
        setDisabled(reason instanceof AdminApiError && reason.status === 404);
        setChecking(false);
      },
    );
    return () => {
      controller.current?.abort();
    };
  }, [adminPath, navigate]);

  async function handleSubmit(event: FormEvent) {
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
        void navigate(`${adminPath}/overview`, { replace: true });
      }
    } catch (reason) {
      if (current.signal.aborted) return;
      if (reason instanceof AdminApiError && reason.status === 404) {
        setDisabled(true);
      } else if (reason instanceof AdminApiError && reason.status === 429) {
        setError("登录尝试过于频繁，请稍后重试。");
      } else {
        setError("管理密码不正确或暂时无法登录。");
      }
    } finally {
      if (!current.signal.aborted) {
        setBusy(false);
        setPassword("");
      }
    }
  }

  return (
    <main className="admin-login-shell">
      <section className="admin-login-card" aria-labelledby="admin-login-title">
        <div className="admin-login-brand">
          <span className="admin-brand-mark" aria-hidden="true">
            A文
          </span>
          <div>
            <div className="admin-brand-name">PDF 保版式翻译</div>
            <div className="admin-brand-sub">站点管理后台</div>
          </div>
        </div>

        <h1 id="admin-login-title">管理登录</h1>
        <p className="admin-login-hint">仅限站点管理员登录查看运行概况、翻译记录并维护服务配置。</p>

        {checking ? (
          <p role="status">正在验证管理会话…</p>
        ) : disabled ? (
          <p role="status">本站未开启管理功能。</p>
        ) : (
          <form className="admin-login-form" onSubmit={(event) => void handleSubmit(event)}>
            {locationState?.expired && (
              <div className="ac-alert warn" role="status">
                管理会话已失效，请重新登录。
              </div>
            )}

            <div className="ac-field">
              <label htmlFor="admin-password">管理密码</label>
              <div className="admin-password-row">
                <input
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
                <button
                  type="button"
                  className="ac-btn"
                  aria-pressed={visible}
                  onClick={() => setVisible((prev) => !prev)}
                >
                  {visible ? "隐藏密码" : "显示密码"}
                </button>
              </div>
            </div>

            {error && (
              <p role="alert" className="ac-field-err">
                {error}
              </p>
            )}

            <button type="submit" className="ac-btn primary" disabled={busy || !password}>
              {busy ? "登录中…" : "登录管理"}
            </button>
          </form>
        )}

        <div className="admin-login-links">
          <Link to="/">返回主站</Link>
          <Link to="/stats">公开统计</Link>
        </div>
      </section>
    </main>
  );
}
