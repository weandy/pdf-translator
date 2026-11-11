import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { createBrowserRouter, Link, Outlet, RouterProvider, useLocation, useOutletContext } from "react-router";

import { authHeaders } from "@/entities/settings/model";
import { usePageVisit } from "@/entities/stats/model";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { GlossaryPage } from "@/pages/GlossaryPage";
import { GuidePage } from "@/pages/GuidePage";
import { HomePage } from "@/pages/HomePage";
import { StatsPage } from "@/pages/StatsPage";
import { api, setAuthHeaders, type ServerConfig } from "@/shared/api/client";

// 把浏览器里保存的 Key 注入接口客户端（shared 层不依赖 entities，由这里连接）
setAuthHeaders(() => authHeaders());

// 阅读器连同 pdf.js、KaTeX 按需加载，首页不用下载它们
const JobPage = lazy(() => import("@/pages/JobPage").then((m) => ({ default: m.JobPage })));
const AdminLoginPage = lazy(() => import("@/pages/AdminPage").then((m) => ({ default: m.AdminLoginPage })));
const AdminSubmissionsPage = lazy(() => import("@/pages/AdminPage").then((m) => ({ default: m.AdminSubmissionsPage })));

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

interface LayoutContext {
  openSettings: () => void;
}

function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const location = useLocation();
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
        <button type="button" className="quiet" onClick={() => setSettingsOpen(true)}>
          设置
        </button>
      </header>
      <Outlet context={{ openSettings: () => setSettingsOpen(true) } satisfies LayoutContext} />
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
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
  const adminPath = (data as ServerConfig & { admin_path?: string }).admin_path;
  const loginPath = `${adminPath}/login`;
  if (!adminPath || (location.pathname !== loginPath && location.pathname !== `${adminPath}/submissions`)) {
    return <AdminNotFound />;
  }
  return (
    <Suspense fallback={<main className="center muted">加载管理页面…</main>}>
      {location.pathname === `${adminPath}/submissions` ? (
        <AdminSubmissionsPage adminPath={adminPath} />
      ) : (
        <AdminLoginPage adminPath={adminPath} />
      )}
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
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
