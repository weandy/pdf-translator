import { useCallback, useId, useState } from "react";

import type { ExportMode } from "@/shared/api/client";

/** 同一次上传共用格式；所有正在准备的下载结束后，才能更换格式。 */
export function useDownloadChoice() {
  const [mode, setMode] = useState<ExportMode>("dual");
  const [pending, setPending] = useState(0);
  const onPreparingChange = useCallback((preparing: boolean) => {
    setPending((count) => count + (preparing ? 1 : -1));
  }, []);
  return { mode, setMode, preparing: pending > 0, onPreparingChange };
}

export function DownloadFormatSelect({
  mode,
  onChange,
  disabled,
  label = "下载格式",
}: {
  mode: ExportMode;
  onChange: (mode: ExportMode) => void;
  disabled?: boolean;
  label?: string;
}) {
  const id = useId();

  return (
    <>
      <label className="visually-hidden" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        value={mode}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value === "mono" ? "mono" : "dual")}
      >
        <option value="dual">双语（左原文·右译文）</option>
        <option value="mono">仅译文</option>
      </select>
    </>
  );
}
