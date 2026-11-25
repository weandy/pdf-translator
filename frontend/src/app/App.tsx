import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import {
  createBrowserRouter,
  Link,
  Navigate,
  Outlet,
  RouterProvider,
  useLocation,
  useOutletContext,
} from "react-router";

import { authHeaders } from "@/entities/settings/model";
import { usePageVisit } from "@/entities/stats/model";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { GlossaryPage } from "@/pages/GlossaryPage";
import { GuidePage } from "@/pages/GuidePage";
import { HomePage } from "@/pages/HomePage";
import { StatsPage } from "@/pages/StatsPage";
import { TermsPage } from "@/pages/TermsPage";
import { api, setAuthHeaders, type ServerConfig } from "@/shared/api/client";
import { ConfirmProvider } from "@/shared/feedback/ConfirmDialog";
import { ToastProvider } from "@/shared/feedback/Toast";

// 阅读器连同 pdf.js、KaTeX 按需加载，首页不用下载它们
const JobPage = lazy(() => import("@/pages/JobPage").then((m) => ({ default: m.JobPage })));
const AdminLoginPage = lazy(() => import("@/pages/AdminPage").then((m) => ({ default: m.AdminLoginPage })));
const AdminConsolePage = lazy(() => import("@/pages/AdminPage").then((m) => ({ default: m.AdminConsolePage })));

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

setAuthHeaders(() => authHeaders(undefined, queryClient.getQueryData<ServerConfig>(["config"])));

interface LayoutContext {
  openSettings: () => void;
}

function Layout() {
  const [settingsRoute, setSettingsRoute] = useState<string | null>(null);
  const location = useLocation();
  if (settingsRoute !== null && settingsRoute !== location.key) setSettingsRoute(null);
  const openSettings = () => setSettingsRoute(location.key);
  useQuery({ queryKey: ["config"], queryFn: api.config });
  usePageVisit(location.key);
  return (
    <>
      <header className="topbar">
        <Link to="/" className="wordmark" aria-label="PDF 保版式翻译，返回首页">
          <span className="wordmark-mark" aria-hidden="true">
            <span>A</span>
            <span>文</span>
          </span>
          <span>
            <span className="wordmark-prefix">PDF </span>保版式翻译
          </span>
        </Link>
        <span className="spacer" />
        <a className="topbar-source" href="https://github.com/weandy/pdf-translator" target="_blank" rel="noreferrer">
          源代码
        </a>
        <Link to="/guide" className="topbar-link">
          <span className="wide-only">使用</span>指南
        </Link>
        <Link to="/glossary" className="topbar-link">
          术语库
        </Link>
        <Link to="/stats" className="topbar-link">
          统计
        </Link>
        <button type="button" className="quiet" onClick={openSettings}>
          设置
        </button>
      </header>
      <Outlet context={{ openSettings } satisfies LayoutContext} />
      {settingsRoute === location.key && <SettingsPanel onClose={() => setSettingsRoute(null)} />}
    </>
  );
}

function Home() {
  const { openSettings } = useOutletContext<LayoutContext>();
  return <HomePage openSettings={openSettings} />;
}

function AdminNotFound() {
  return <main className="center muted">页面不存在</main>;
}

function ConfiguredAdminRoute() {
  const location = useLocation();
  const { data, isPending, isError } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
    retry: false,
  });
  if (isPending) return <main className="center muted">正在读取管理入口…</main>;
  if (isError || !data) return <main className="center muted">暂时无法读取管理入口配置，请稍后重试。</main>;
  const config = data as ServerConfig;
  const adminPath = config.admin_path?.replace(/\/+$/, "");
  if (!adminPath) return <AdminNotFound />;
  const normalizedPath = location.pathname.length > 1 ? location.pathname.replace(/\/+$/, "") : location.pathname;
  if (normalizedPath === adminPath) {
    return <Navigate to={`${adminPath}/overview`} replace />;
  }
  if (normalizedPath === `${adminPath}/login`) {
    return (
      <Suspense fallback={<main className="center muted">加载管理页面…</main>}>
        <AdminLoginPage adminPath={adminPath} />
      </Suspense>
    );
  }
  let section: "overview" | "records" | "services" | "settings" | null = null;
  if (normalizedPath === `${adminPath}/overview`) section = "overview";
  else if (normalizedPath === `${adminPath}/records` || normalizedPath === `${adminPath}/submissions`) {
    section = "records";
  } else if (normalizedPath === `${adminPath}/services`) section = "services";
  else if (normalizedPath === `${adminPath}/settings`) section = "settings";
  if (!section) return <AdminNotFound />;
  return (
    <Suspense fallback={<main className="center muted">加载管理页面…</main>}>
      <AdminConsolePage
        adminPath={adminPath}
        section={section}
        submissionAuditEnabled={config.submission_audit_enabled}
      />
    </Suspense>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Home /> },
      { path: "/guide", element: <GuidePage /> },
      { path: "/glossary", element: <GlossaryPage /> },
      { path: "/stats", element: <StatsPage /> },
      { path: "/tos", element: <TermsPage /> },
      {
        path: "/jobs/:id",
        element: (
          <Suspense fallback={<main className="center muted">加载中…</main>}>
            <JobPage />
          </Suspense>
        ),
      },
    ],
  },
  // 兜底路由必须放在公共路由之后，避免管理入口抢占 /stats、/guide 等页面。
  { path: "*", element: <ConfiguredAdminRoute /> },
]);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ConfirmProvider>
          <RouterProvider router={router} />
        </ConfirmProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}
