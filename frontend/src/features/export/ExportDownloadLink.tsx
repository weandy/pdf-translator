import { useState, type MouseEvent, type ReactNode } from "react";

import { api, exportUrl, type ExportMode } from "@/shared/api/client";

type DownloadState = "idle" | "preparing" | "started" | "error";

export type ExportDownloadState = DownloadState;

interface Props {
  jobId: string;
  mode: ExportMode;
  side?: "left" | "right";
  children: ReactNode;
  role?: "menuitem";
  className?: string;
  disabled?: boolean;
  onStateChange?: (state: ExportDownloadState, message: string) => void;
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
  onStateChange,
}: Props) {
  const [state, setState] = useState<DownloadState>("idle");
  const [error, setError] = useState("");

  const start = async (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (disabled || state === "preparing" || state === "started") return;
    setState("preparing");
    setError("");
    onStateChange?.("preparing", "");
    try {
      const prepared = await api.prepareExport(jobId, mode, side);
      const link = document.createElement("a");
      link.href = prepared.download_url;
      link.download = prepared.filename;
      link.click();
      setState("started");
      onStateChange?.("started", "");
    } catch (cause) {
      setState("error");
      const message = cause instanceof Error ? cause.message : "PDF 准备失败，请重试";
      setError(message);
      onStateChange?.("error", message);
    }
  };

  const label =
    state === "preparing"
      ? "正在准备 PDF…"
      : state === "started"
        ? "已开始下载"
        : state === "error"
          ? "重试下载"
          : children;
  const hint =
    state === "preparing"
      ? "正在排版并准备文件，请不要重复点击"
      : state === "started"
        ? "请查看浏览器下载栏"
        : state === "error"
          ? error
          : null;

  return (
    <a
      role={role}
      className={`${className ?? ""}${disabled || state === "preparing" || state === "started" ? " disabled" : ""}`}
      href={exportUrl(jobId, mode, side)}
      download
      aria-busy={state === "preparing"}
      aria-disabled={disabled || state === "preparing" || state === "started" ? true : undefined}
      title={error || undefined}
      onClick={start}
    >
      {state === "idle" ? label : <span>{label}</span>}
      {hint && <small>{hint}</small>}
    </a>
  );
}
