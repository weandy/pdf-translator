import { useQuery } from "@tanstack/react-query";
import { useId, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";

import { api, type Expansion, type Job } from "@/shared/api/client";
import { parsePageRange } from "@/shared/text/pageRange";

import "./expansion.css";

interface Props {
  job: Job;
  published: ReadonlySet<number>;
  onClose: () => void;
  onStarted: (op: Expansion) => void;
}

export function ExpandPagesDialog({ job, published, onClose, onStarted }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const close = useRef(onClose);
  const sending = useRef(false);
  const [specific, setSpecific] = useState(false);
  const [range, setRange] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const title = useId();
  const rangeId = useId();
  const remaining = job.page_count - published.size;
  const parsed = specific ? parsePageRange(range, job.page_count) : null;
  const delta = Array.isArray(parsed) ? parsed.filter((page) => !published.has(page)) : [];
  const rangeError = specific && parsed && !Array.isArray(parsed)
    ? parsed.error
    : specific && parsed && delta.length === 0 ? "所选页面已全部翻译" : "";
  const count = specific ? delta.length : remaining;
  const paid = job.channel_mode !== "free";
  const { data: estimate } = useQuery({
    queryKey: ["estimate", count, job.options.quality, job.model],
    queryFn: () => api.estimate(count, job.options.quality, job.model || undefined),
    enabled: paid && count > 0,
  });

  useLayoutEffect(() => {
    close.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement;
    if (!element) return;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!sending.current) close.current();
    };
    if (typeof element.showModal === "function") element.showModal();
    else {
      element.setAttribute("open", "");
      element.dataset.fallback = "";
      cancelButton.current?.focus();
      window.addEventListener("keydown", escape, true);
    }
    return () => {
      window.removeEventListener("keydown", escape, true);
      if (typeof element.close === "function") element.close();
      else element.removeAttribute("open");
      document.body.style.overflow = overflow;
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (sending.current || count === 0) return;
    sending.current = true;
    setBusy(true);
    setFailure("");
    try {
      const started = await api.expandPages(job.id, specific ? delta : null, job.channel_mode);
      onStarted(started);
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  return createPortal(
    <>
      <dialog
        ref={dialog}
        className="confirm-dialog"
        aria-modal="true"
        aria-labelledby={title}
        onCancel={(event) => {
          event.preventDefault();
          if (!sending.current) onClose();
        }}
        onClick={(event) => {
          if (event.target === event.currentTarget && !sending.current) onClose();
        }}
      >
        <form className="confirm-content expansion-form" onSubmit={(event) => void submit(event)}>
          <h2 id={title}>补翻其余页</h2>
          <p className="hint">补翻期间仍可阅读和下载已有译文。补翻结束后可继续编辑。</p>
          <fieldset disabled={busy}>
            <legend>选择要补翻的页面</legend>
            <label><input type="radio" checked={!specific} onChange={() => setSpecific(false)} /> 补翻全部剩余 {remaining} 页</label>
            <label><input type="radio" checked={specific} onChange={() => setSpecific(true)} /> 补翻指定页码范围</label>
            {specific && (
              <label htmlFor={rangeId}>
                页码范围
                <input id={rangeId} value={range} onChange={(event) => setRange(event.target.value)}
                  placeholder="例如 2-5, 8" aria-invalid={Boolean(rangeError)} />
              </label>
            )}
          </fieldset>
          {rangeError && <p className="error" role="alert">{rangeError}</p>}
          {count > 0 && (
            <p className="hint" aria-live="polite">
              实际补翻 {count} 页
              {!paid ? " · 免费通道" : estimate?.cny_low != null
                ? ` · 预计 ¥${estimate.cny_low.toFixed(2)}–${(estimate.cny_high ?? estimate.cny_low).toFixed(2)}`
                : " · 暂无费用估算"}
            </p>
          )}
          {failure && <p className="error" role="alert">{failure}</p>}
          <div className="confirm-actions">
            <button type="button" ref={cancelButton} disabled={busy} onClick={onClose}>取消</button>
            <button type="submit" className="primary" disabled={busy || count === 0}>
              {busy ? "正在提交…" : "开始补翻"}
            </button>
          </div>
        </form>
      </dialog>
      <div className="confirm-fallback" aria-hidden="true" onClick={() => { if (!sending.current) onClose(); }} />
    </>,
    document.body,
  );
}
