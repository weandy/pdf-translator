/**
 * 下载译文 PDF：纯译文版（原版式，正文换成中文）或双语对照版（左原文右译文）。
 * 每次下载都按当前译文生成，包含在阅读器里做的修改。
 */
import { useCallback, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useAnchoredMenu } from "@/shared/feedback/useAnchoredMenu";

import { ExportDownloadLink, ExportDownloadStatus, type ExportDownloadState } from "./ExportDownloadLink";

interface Props {
  jobId: string;
  contentVersion?: number;
  onLocatePage?: (page: number) => void;
}

export function DownloadMenu(props: Props) {
  return <JobDownloadMenu key={`${props.jobId}:${props.contentVersion ?? 1}`} {...props} />;
}

function JobDownloadMenu({ jobId, onLocatePage }: Props) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ExportDownloadState>("idle");
  const [message, setMessage] = useState("");
  const [pages, setPages] = useState<number[]>([]);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const statusId = useId();
  const close = useCallback(() => setOpen(false), []);
  const menu = useAnchoredMenu(open, trigger, close, "right", root);

  useLayoutEffect(() => {
    if (open) menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus({ preventScroll: true });
  }, [open, menu]);

  const onStateChange = (next: ExportDownloadState, detail: string, overflow: number[]) => {
    setPages(overflow);
    setState(next);
    setMessage(detail);
  };
  const busy = state === "preparing";
  const menuItemProps = { role: "menuitem" as const, jobId, disabled: busy, showStatus: false, onStateChange };

  return (
    <div className="download" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="primary toolbar-download-btn"
        disabled={busy}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-busy={busy}
        aria-describedby={statusId}
        onClick={() => setOpen((o) => !o)}
      >
        <svg className="ui-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
        <span>下载 PDF</span>
      </button>
      <div className="download-status-anchor">
        <ExportDownloadStatus
          id={statusId}
          state={state}
          message={message}
          pages={pages}
          renderOverflowPage={onLocatePage && ((page) => (
            <button type="button" className="link" aria-label={`定位到第 ${page} 页`} onClick={() => onLocatePage(page)}>
              {page}
            </button>
          ))}
        />
      </div>
      {(open || busy) && createPortal(
        <div ref={menu} className="download-menu" role={open ? "menu" : undefined} hidden={!open}
          style={{ position: "fixed" }}>
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
        </div>,
        document.body,
      )}
    </div>
  );
}
