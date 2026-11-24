import { useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from "react";

import { api, exportUrl, type ExportMode } from "@/shared/api/client";

export type ExportDownloadState = "idle" | "preparing" | "started" | "error";

interface Props {
  jobId: string;
  mode: ExportMode;
  side?: "left" | "right";
  children: ReactNode;
  role?: "menuitem";
  className?: string;
  disabled?: boolean;
  showStatus?: boolean;
  onStateChange?: (state: ExportDownloadState, message: string) => void;
  onPreparingChange?: (preparing: boolean) => void;
}

/** 先准备服务端 PDF，再启动浏览器下载，避免慢生成时用户重复点击。 */
export function ExportDownloadLink({
  jobId,
  mode,
  side = "right",
  children,
  role,
  className,
  disabled = false,
  showStatus = true,
  onStateChange,
  onPreparingChange,
}: Props) {
  const statusId = useId();
  const [feedback, setFeedback] = useState<{ state: ExportDownloadState; message: string }>({
    state: "idle",
    message: "",
  });
  const releasePreparing = useRef<(() => void) | null>(null);
  const preparing = feedback.state === "preparing";
  const blocked = disabled || preparing;

  useEffect(() => () => releasePreparing.current?.(), []);

  const report = (state: ExportDownloadState, message = "") => {
    setFeedback({ state, message });
    onStateChange?.(state, message);
  };

  const start = async (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (blocked || releasePreparing.current) return;

    // 同步加锁，避免界面重新渲染前的点击重复提交；卸载时释放上层格式选择。
    const release = () => {
      if (releasePreparing.current !== release) return;
      releasePreparing.current = null;
      onPreparingChange?.(false);
    };
    releasePreparing.current = release;
    onPreparingChange?.(true);
    report("preparing");
    try {
      const prepared = await api.prepareExport(jobId, mode, side);
      const link = document.createElement("a");
      link.href = prepared.download_url;
      link.download = prepared.filename;
      link.click();
      const pages = prepared.overflow_pages ?? [];
      const firstPages = pages.slice(0, 5).join("、");
      const pageSummary = pages.length > 5 ? `第 ${firstPages} 页等共 ${pages.length} 页` : `第 ${firstPages} 页`;
      report("started", pages.length ? `已开始下载，${pageSummary}部分译文未写入 PDF` : "");
    } catch (cause) {
      report("error", cause instanceof Error ? cause.message : "PDF 准备失败，请重试");
    } finally {
      release();
    }
  };

  return (
    <>
      <a
        role={role}
        className={`${className ?? ""}${blocked ? " disabled" : ""}`}
        href={exportUrl(jobId, mode, side)}
        download
        aria-busy={preparing}
        aria-disabled={blocked || undefined}
        aria-describedby={showStatus ? statusId : undefined}
        onClick={start}
      >
        {children}
      </a>
      {showStatus && <ExportDownloadStatus id={statusId} {...feedback} />}
    </>
  );
}

export function ExportDownloadStatus({
  id,
  state,
  message,
}: {
  id: string;
  state: ExportDownloadState;
  message: string;
}) {
  const statusText = {
    idle: "",
    preparing: "正在准备 PDF…",
    started: message || "已开始下载",
    error: message || "下载失败，请重试",
  }[state];

  return (
    <small
      id={id}
      className={`export-download-status${state === "error" ? " error" : ""}`}
      role="status"
      aria-atomic="true"
    >
      {statusText}
    </small>
  );
}
