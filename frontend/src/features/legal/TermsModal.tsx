import { useEffect } from "react";
import { Link } from "react-router";

import type { ServerConfig } from "@/shared/api/client";
import { TermsContent } from "@/shared/legal/TermsContent";

interface Props {
  config?: ServerConfig;
  onClose: () => void;
}

export function TermsModal({ config, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="tos-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="tos-modal" role="dialog" aria-modal="true" aria-labelledby="tos-modal-title">
        <div className="tos-modal-head">
          <div>
            <h2 id="tos-modal-title">服务条款与隐私说明（ToS）</h2>
            <p className="hint">数据流转、保留期限、本机密钥与提交审计说明</p>
          </div>
          <button type="button" className="quiet" onClick={onClose} aria-label="关闭条款弹窗">
            关闭
          </button>
        </div>

        <div className="tos-modal-body">
          <TermsContent
            retentionHours={config?.retention_hours}
            submissionAuditEnabled={config?.submission_audit_enabled}
          />
        </div>

        <div className="tos-modal-foot">
          <div className="tos-modal-links">
            <Link to="/tos" onClick={onClose}>
              打开独立条款页（/tos）
            </Link>
            <Link to="/guide#privacy" onClick={onClose}>
              使用指南 · 隐私节
            </Link>
          </div>
          <button type="button" className="primary" onClick={onClose}>
            我已了解
          </button>
        </div>
      </div>
    </div>
  );
}
