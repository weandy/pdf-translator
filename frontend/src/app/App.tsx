import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { createBrowserRouter, Link, Outlet, RouterProvider, useOutletContext } from "react-router";

import { authHeaders } from "@/entities/settings/model";
import { GlossaryPanel } from "@/features/glossary/GlossaryPanel";
import { SettingsPanel } from "@/features/settings/SettingsPanel";
import { HomePage } from "@/pages/HomePage";
import { setAuthHeaders } from "@/shared/api/client";

// 把浏览器里保存的 Key 注入接口客户端（shared 层不依赖 entities，由这里连接）
setAuthHeaders(() => authHeaders());

// 阅读器连同 pdf.js、KaTeX 按需加载，首页不用下载它们
const JobPage = lazy(() => import("@/pages/JobPage").then((m) => ({ default: m.JobPage })));

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

interface LayoutContext {
  openSettings: () => void;
}

function Layout() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [glossaryOpen, setGlossaryOpen] = useState(false);
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
        <button type="button" className="quiet" onClick={() => setGlossaryOpen(true)}>
          术语库
        </button>
        <button type="button" className="quiet" onClick={() => setSettingsOpen(true)}>
          设置
        </button>
      </header>
      <Outlet context={{ openSettings: () => setSettingsOpen(true) } satisfies LayoutContext} />
      {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
      {glossaryOpen && <GlossaryPanel onClose={() => setGlossaryOpen(false)} />}
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
