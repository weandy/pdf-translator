/**
 * 下载译文 PDF：纯译文版（原版式，正文换成中文）或双语对照版（左原文右译文）。
 * 每次下载都按当前译文生成，包含在阅读器里做的修改。
 */
import { useEffect, useRef, useState } from "react";

import { exportUrl } from "@/shared/api/client";

export function DownloadMenu({ jobId }: { jobId: string }) {
  const [open, setOpen] = useState(false);
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

  return (
    <div className="download" ref={root}>
      <button type="button" className="primary" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        下载 PDF
      </button>
      {open && (
        <div className="download-menu" role="menu">
          <a role="menuitem" href={exportUrl(jobId, "mono")} download onClick={() => setOpen(false)}>
            <span>中文版</span>
            <small>保留原版式，正文换成译文</small>
          </a>
          <a role="menuitem" href={exportUrl(jobId, "dual")} download onClick={() => setOpen(false)}>
            <span>双语对照版</span>
            <small>每页左边原文、右边译文</small>
          </a>
        </div>
      )}
    </div>
  );
}
