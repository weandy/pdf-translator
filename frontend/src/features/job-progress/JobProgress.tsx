import { useState } from "react";

import { STAGE_LABELS, STAGE_ORDER } from "@/entities/job/model";
import { api, type Job } from "@/shared/api/client";

interface Props {
  job: Job;
  onRetried: () => void;
}

export function JobProgress({ job, onRetried }: Props) {
  const [error, setError] = useState<string | null>(null);
  const current = STAGE_ORDER.indexOf(job.stage);

  const retry = async () => {
    setError(null);
    try {
      await api.retryJob(job.id);
      onRetried();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="progress" aria-live="polite">
      <h2>{job.filename}</h2>
      <ol className="stages">
        {STAGE_ORDER.map((stage, i) => {
          const state =
            job.status === "done" || i < current ? "done" : i === current ? (job.status === "failed" ? "failed" : "active") : "";
          return (
            <li key={stage} className={state}>
              {STAGE_LABELS[stage]}
              {state === "active" && stage === "translate" && job.progress > 0 && ` ${Math.round(job.progress * 100)}%`}
            </li>
          );
        })}
      </ol>
      {job.message && <p className="hint">{job.message}</p>}
      {job.status === "failed" && (
        <div className="error">
          <p>{job.error || "任务失败"}</p>
          <button type="button" onClick={() => void retry()}>
            从断点重试
          </button>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
