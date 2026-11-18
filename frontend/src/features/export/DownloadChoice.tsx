import { useId, useState } from "react";

import type { ExportMode } from "@/shared/api/client";

import { ExportDownloadLink, type ExportDownloadState } from "./ExportDownloadLink";

/** 任务和批次共用下载类型，默认按左原文、右译文生成双语 PDF。 */
export function DownloadChoice({ jobId }: { jobId: string }) {
  const id = useId();
  const [mode, setMode] = useState<ExportMode>("dual");
  const [state, setState] = useState<ExportDownloadState>("idle");
  const preparing = state === "preparing";

  return (
    <div className="download-choice">
      <label className="visually-hidden" htmlFor={id}>
        下载类型
      </label>
      <select
        id={id}
        value={mode}
        disabled={preparing}
        onChange={(event) => {
          setMode(event.target.value as ExportMode);
          setState("idle");
        }}
      >
        <option value="dual">双语（左原文·右译文）</option>
        <option value="mono">仅译文</option>
      </select>
      <ExportDownloadLink
        key={`${jobId}-${mode}`}
        className="button quiet"
        jobId={jobId}
        mode={mode}
        side="right"
        onStateChange={setState}
      >
        下载 PDF
      </ExportDownloadLink>
    </div>
  );
}
