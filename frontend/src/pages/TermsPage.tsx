import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router";

import { api } from "@/shared/api/client";
import { TermsContent } from "@/shared/legal/TermsContent";

export function TermsPage() {
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
  });

  useEffect(() => {
    document.title = "服务条款与隐私说明";
  }, []);

  return (
    <main className="tos-page">
      <article className="tos-sheet">
        <header className="tos-sheet-head">
          <h1>服务条款与隐私说明（ToS）</h1>
          <p className="lead">说明 PDF 版面解析、正文翻译传输路径、文件保留期限、本机凭据与站点审计边界。</p>
        </header>

        <TermsContent
          retentionHours={config?.retention_hours}
          submissionAuditEnabled={config?.submission_audit_enabled}
        />

        <footer className="tos-sheet-foot">
          <Link to="/">返回首页开始翻译</Link>
          <Link to="/guide#privacy">查看使用指南</Link>
        </footer>
      </article>
    </main>
  );
}
