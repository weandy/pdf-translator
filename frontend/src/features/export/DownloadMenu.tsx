/**
 * 下载译文 PDF：纯译文版（原版式，正文换成中文）或双语对照版（左原文右译文）。
 * 每次下载都按当前译文生成，包含在阅读器里做的修改。
 */
import { useEffect, useRef, useState } from "react";

import { ExportDownloadLink, type ExportDownloadState } from "@/features/export/ExportDownloadLink";

export function DownloadMenu({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ExportDownloadState>("idle");
  const [message, setMessage] = useState("");
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const onStateChange = (next: ExportDownloadState, detail: string) => {
    setState(next);
    setMessage(detail);
  };
  const busy = state === "preparing" || state === "started";
  const label =
    state === "preparing"
      ? "正在准备 PDF…"
      : state === "started"
        ? "已开始下载"
        : state === "error"
          ? "下载失败，重试"
          : "下载 PDF";

  return (
    <div className="download" ref={root}>
      <button
        type="button"
        className={`primary${busy ? " disabled" : ""}`}
        aria-expanded={open}
        aria-busy={state === "preparing"}
        onClick={() => {
          if (!busy) setOpen((o) => !o);
        }}
      >
        <span>
          {label}
        </span>
      </button>
      {state !== "idle" && <small className={`download-status${state === "error" ? " error" : ""}`}>{message || (state === "started" ? "请查看浏览器下载栏" : "正在排版并准备文件，请不要重复点击")}</small>}
      {open && (
        <div className="download-menu" role="menu">
          <ExportDownloadLink role="menuitem" jobId={jobId} mode="mono" disabled={busy} onStateChange={onStateChange}>
            <span>中文版</span>
            <small>保留原版式，正文换成译文</small>
          </ExportDownloadLink>
          <ExportDownloadLink role="menuitem" jobId={jobId} mode="dual" side="right" disabled={busy} onStateChange={onStateChange}>
            <span>双语对照版（译文在右）</span>
            <small>每页左边原文、右边译文</small>
          </ExportDownloadLink>
          <ExportDownloadLink role="menuitem" jobId={jobId} mode="dual" side="left" disabled={busy} onStateChange={onStateChange}>
            <span>双语对照版（译文在左）</span>
            <small>每页左边译文、右边原文</small>
          </ExportDownloadLink>
        </div>
      )}
    </div>
  );
}
