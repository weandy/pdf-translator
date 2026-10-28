import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { createBrowserRouter, Outlet, RouterProvider, useNavigate, useOutletContext } from "react-router";

import { authHeaders } from "@/entities/settings/model";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { HomePage } from "@/pages/HomePage";
import { setAuthHeaders } from "@/shared/api/client";

// 把浏览器里保存的 Key 注入接口客户端（shared 层不依赖 entities，由这里连接）
setAuthHeaders(() => authHeaders());

// 阅读器连同 pdf.js、KaTeX 按需加载，首页不用下载它们
const JobPage = lazy(() => import("@/pages/JobPage").then((m) => ({ default: m.JobPage })));

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

interface LayoutContext {
  openSettings: () => void;
}

function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const navigate = useNavigate();
  return (
    <>
      <header className="topbar">
        <button type="button" className="brand link" onClick={() => void navigate("/")}>
          PDF 保版式翻译
        </button>
        <span className="spacer" />
        <a href="https://github.com/weandy/pdf-translator" target="_blank" rel="noreferrer">
          GitHub
        </a>
        <button type="button" onClick={() => setSettingsOpen(true)}>
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

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <Home /> },
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
]);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
