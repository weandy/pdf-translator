import { useId, useState } from "react";

import type { ExportMode } from "@/shared/api/client";

import { ExportDownloadLink } from "./ExportDownloadLink";

export const DEFAULT_DOWNLOAD_MODE: ExportMode = "dual";

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

/** 默认按左原文、右译文生成双语 PDF。 */
export function DownloadChoice({ jobId }: { jobId: string }) {
  const [mode, setMode] = useState<ExportMode>(DEFAULT_DOWNLOAD_MODE);
  const [preparing, setPreparing] = useState(false);

  return (
    <div className="download-choice">
      <DownloadFormatSelect mode={mode} onChange={setMode} disabled={preparing} />
      <div className="download-choice-action">
        <ExportDownloadLink
          key={`${jobId}-${mode}`}
          className="button quiet"
          jobId={jobId}
          mode={mode}
          onPreparingChange={setPreparing}
        >
          下载 PDF
        </ExportDownloadLink>
      </div>
    </div>
  );
}
