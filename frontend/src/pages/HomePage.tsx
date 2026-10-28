import { useEffect, useState } from "react";

import { fetchHealth, type Health } from "@/shared/api/client";

type State = { kind: "loading" } | { kind: "ok"; health: Health } | { kind: "error"; message: string };

export function HomePage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    fetchHealth(ctrl.signal)
      .then((health) => setState({ kind: "ok", health }))
      .catch((e: unknown) => {
        if (!ctrl.signal.aborted) setState({ kind: "error", message: String(e) });
      });
    return () => ctrl.abort();
  }, []);

  return (
    <main className="home">
      <h1>PDF 保版式翻译</h1>
      <p className="lead">上传英文论文 PDF，按原版式对照阅读中文译文。</p>
      <section className="status" aria-live="polite">
        {state.kind === "loading" && <span>正在连接后端…</span>}
        {state.kind === "ok" && (
          <span className="ok">
            后端已连接 · v{state.health.version}
            {!state.health.data_dir_writable && "（数据目录不可写）"}
          </span>
        )}
        {state.kind === "error" && <span className="err">后端不可用：{state.message}</span>}
      </section>
      <p className="note">上传与阅读器功能开发中（MVP 第一阶段）。</p>
    </main>
  );
}
