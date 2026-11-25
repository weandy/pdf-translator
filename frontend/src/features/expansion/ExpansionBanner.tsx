import { useRef, useState } from "react";

import { STAGE_LABELS } from "@/entities/job/model";
import { api, type Expansion } from "@/shared/api/client";
import { useConfirm } from "@/shared/feedback/ConfirmDialog";
import { useToast } from "@/shared/feedback/Toast";
import { formatDuration, useNow } from "@/shared/time/elapsed";

import "./expansion.css";

export function ExpansionBanner({ op }: { op: Expansion }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [cancelling, setCancelling] = useState(false);
  const pending = useRef(false);
  const now = useNow(true);
  const cancel = async () => {
    if (pending.current) return;
    pending.current = true;
    try {
      if (!(await confirm({
        title: "取消补翻",
        message: "取消后仍可阅读和下载已发布的译文。已产生的翻译费用照常结算。",
        confirmLabel: "确认取消补翻",
        tone: "danger",
      }))) return;
      setCancelling(true);
      await api.cancelExpansion(op.job_id, op.id);
      toast.info("已提交取消请求，正在停止补翻…");
    } catch (error) {
      setCancelling(false);
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      pending.current = false;
    }
  };

  const stage = op.stage === "publish" ? "发布译文" : STAGE_LABELS[op.stage] ?? "正在准备";
  return (
    <section className="expansion-banner" aria-label="补翻进度">
      <div role="status">
        <strong>正在补翻 {op.requested_pages.length} 页</strong>
        <span>{stage} · {op.message} · 已等待 {formatDuration(now - Date.parse(op.created_at))}</span>
      </div>
      <progress max={1} value={op.progress} aria-label="当前阶段进度" />
      <button type="button" className="quiet" disabled={cancelling} onClick={() => void cancel()}>
        {cancelling ? "正在取消…" : "取消补翻"}
      </button>
    </section>
  );
}
