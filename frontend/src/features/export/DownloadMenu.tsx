/**
 * 下载译文 PDF：纯译文版（原版式，正文换成中文）或双语对照版（左原文右译文）。
 * 每次下载都按当前译文生成，包含在阅读器里做的修改。
 */
import { useEffect, useId, useRef, useState } from "react";

import { ExportDownloadLink, ExportDownloadStatus, type ExportDownloadState } from "./ExportDownloadLink";

export function DownloadMenu({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ExportDownloadState>("idle");
  const [message, setMessage] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const statusId = useId();

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
  const busy = state === "preparing";
  const menuItemProps = { role: "menuitem" as const, jobId, disabled: busy, showStatus: false, onStateChange };

  return (
    <div className="download" ref={root}>
      <button
        type="button"
        className="primary"
        disabled={busy}
        aria-expanded={open}
        aria-busy={busy}
        aria-describedby={statusId}
        onClick={() => setOpen((o) => !o)}
      >
        下载 PDF
      </button>
      <ExportDownloadStatus id={statusId} state={state} message={message} />
      {open && (
        <div className="download-menu" role="menu">
          <ExportDownloadLink {...menuItemProps} mode="mono">
            <span>中文版</span>
            <small>保留原版式，正文换成译文</small>
          </ExportDownloadLink>
          <ExportDownloadLink {...menuItemProps} mode="dual" side="right">
            <span>双语对照版（译文在右）</span>
            <small>每页左边原文、右边译文</small>
          </ExportDownloadLink>
          <ExportDownloadLink {...menuItemProps} mode="dual" side="left">
            <span>双语对照版（译文在左）</span>
            <small>每页左边译文、右边原文</small>
          </ExportDownloadLink>
        </div>
      )}
    </div>
  );
}
