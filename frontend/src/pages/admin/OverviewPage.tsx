import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import { useAdminConsole } from "@/pages/admin/AdminLayout";
import {
  adminApi,
  AdminApiError,
  type AdminOverview,
  type ClientBucket,
  type QueueSlotStats,
} from "@/shared/api/admin";

const DEVICE_LABELS: Record<string, { label: string; color: string }> = {
  desktop: { label: "桌面", color: "var(--ac-pen)" },
  mobile: { label: "手机", color: "var(--ac-ok)" },
  tablet: { label: "平板", color: "var(--ac-warn)" },
  bot: { label: "脚本", color: "var(--ac-err)" },
  unknown: { label: "其他", color: "var(--ac-faint)" },
};

const STAGE_LABELS: Record<string, string> = {
  parse: "版面解析",
  fuse: "对齐文字",
  segment: "切分段落",
  glossary: "抽取术语",
  translate: "翻译中",
  render: "文档生成",
  done: "已完成",
};

const jobDateTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatJobTime(iso?: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "" : jobDateTime.format(parsed);
}

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function QueueCard({ title, stats, footnote }: { title: string; stats: QueueSlotStats; footnote: string }) {
  const ratio = stats.limit > 0 ? Math.min(100, Math.round((stats.in_use / stats.limit) * 100)) : 0;
  const barTone = ratio >= 100 ? "warn" : "ok";
  const badgeTone = stats.waiting > 0 ? "pen" : stats.in_use > 0 ? "ok" : "";
  const badgeText = stats.waiting > 0 ? `${stats.waiting} 等待` : stats.in_use > 0 ? "运行中" : "空闲";

  return (
    <article className="ac-queue-card">
      <div className="ac-queue-top">
        <span>{title}</span>
        <span className={`ac-badge${badgeTone ? ` ${badgeTone}` : ""}`}>{badgeText}</span>
      </div>
      <div className="ac-queue-num">
        {stats.in_use} <small>/ {stats.limit} 并发</small>
      </div>
      <div className="ac-bar" aria-hidden="true">
        <span className={barTone} style={{ width: `${ratio}%` }} />
      </div>
      <div className="ac-queue-foot">
        等待中：{stats.waiting} · {footnote}
      </div>
    </article>
  );
}

function deviceMeta(bucket: ClientBucket) {
  return DEVICE_LABELS[bucket.label] ?? { label: bucket.label, color: "var(--ac-faint)" };
}

function formatShare(share: number): string {
  const pct = share <= 1 ? share * 100 : share;
  return `${Math.round(pct)}%`;
}

export function OverviewPage() {
  const { adminPath, handleUnauthorized, syncSummary } = useAdminConsole();
  const navigate = useNavigate();
  const [data, setData] = useState<AdminOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const controllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError("");
    try {
      const overview = await adminApi.overview(controller.signal);
      if (controller.signal.aborted) return;
      setData(overview);
      syncSummary({
        revision: overview.revision,
        secretKeyReady: overview.secret_key_ready,
        maintenanceMode: overview.maintenance_mode,
        warnings: overview.warnings,
        failed24h: overview.stats.failed_24h,
      });
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (reason instanceof AdminApiError && reason.status === 401) {
        handleUnauthorized();
        return;
      }
      setError(reason instanceof AdminApiError ? reason.message : "读取运行概况失败，请重试。");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [handleUnauthorized, syncSummary]);

  useEffect(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    void adminApi.overview(controller.signal).then(
      (overview) => {
        if (controller.signal.aborted) return;
        setData(overview);
        setError("");
        setLoading(false);
        syncSummary({
          revision: overview.revision,
          secretKeyReady: overview.secret_key_ready,
          maintenanceMode: overview.maintenance_mode,
          warnings: overview.warnings,
          failed24h: overview.stats.failed_24h,
        });
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof AdminApiError && reason.status === 401) {
          handleUnauthorized();
          return;
        }
        setError(reason instanceof AdminApiError ? reason.message : "读取运行概况失败，请重试。");
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [handleUnauthorized, syncSummary]);

  const breakdown = data?.stats.client_breakdown;
  const devices = breakdown?.devices ?? [];
  const platforms = breakdown?.platforms ?? [];
  const hasBreakdown = Boolean(breakdown && breakdown.total > 0 && devices.length > 0);
  const breakdownSource = data?.stats.client_breakdown_source;
  const isVisitBreakdown = breakdownSource === "visit";
  const breakdownLabel =
    breakdownSource === "visit" ? "访问终端分布" : breakdownSource === "job" ? "提交终端分布" : "终端分布";
  const breakdownTitle = `${breakdownLabel}（近 7 日）`;
  const breakdownUnit = isVisitBreakdown ? "次" : "篇";
  const parseFootnote = data
    ? data.queues.parse_reserved_for_byok
      ? "为自备 Key 预留 1 席"
      : data.queues.parse.limit <= 1
        ? "当前解析并发为 1，无独立预留"
        : "未启用独立预留"
    : "";
  const firstFailure = data?.stats.recent_failures?.[0];

  return (
    <>
      <div className="ac-actions" style={{ justifyContent: "space-between" }}>
        <span className="ac-hint">只读运行总览：实时展示队列占用、{breakdownLabel}、核心服务状态与当前在途任务。</span>
        <button type="button" className="ac-btn sm" disabled={loading} onClick={() => void load()}>
          {loading ? "刷新中…" : "刷新概况"}
        </button>
      </div>

      {error && (
        <div className="ac-alert err" role="alert">
          <span>{error}</span>
          <button type="button" className="ac-btn sm" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {data && data.warnings.length > 0 && (
        <div className="ac-alert warn" role="alert">
          <div>
            <strong>配置解密告警：</strong>
            {data.warnings.join("；")}
          </div>
          <Link to={`${adminPath}/services`} className="ac-btn sm">
            前往服务设置修复
          </Link>
        </div>
      )}

      {data && (
        <>
          <section className="ac-queue-grid" aria-label="实时任务队列">
            <QueueCard title="版面解析队列" stats={data.queues.parse} footnote={parseFootnote} />
            <QueueCard title="免费翻译队列" stats={data.queues.free} footnote="公共免费通道独立调度" />
            <QueueCard title="自备 Key 翻译队列" stats={data.queues.byok} footnote="自备 Key 独立配额" />
          </section>

          <section className="ac-ov-split">
            <article className="ac-card">
              <div className="ac-card-hd">
                <div>
                  <h2 className="ac-card-title">{breakdownTitle}</h2>
                  <p className="ac-card-sub">
                    {isVisitBreakdown
                      ? "按访问记录汇总桌面、手机、平板与脚本分布"
                      : "按论文提交记录汇总桌面、手机、平板与脚本分布"}
                  </p>
                </div>
                {hasBreakdown && (
                  <span className="ac-badge mono">
                    {breakdown!.total} {breakdownUnit}
                  </span>
                )}
              </div>

              {hasBreakdown ? (
                <>
                  <div className="ac-ua-bar" role="img" aria-label={`近 7 日${breakdownLabel}占比条`}>
                    {devices.map((item) => {
                      const meta = deviceMeta(item);
                      const pct = item.share <= 1 ? item.share * 100 : item.share;
                      return (
                        <span
                          key={item.label}
                          style={{ width: `${Math.max(pct, 1)}%`, background: meta.color }}
                          title={`${meta.label} ${formatShare(item.share)}`}
                        />
                      );
                    })}
                  </div>
                  <div className="ac-ua-legend">
                    {devices.map((item) => {
                      const meta = deviceMeta(item);
                      return (
                        <span key={item.label}>
                          <i className="ac-ua-swatch" style={{ background: meta.color }} aria-hidden="true" />
                          {meta.label} {formatShare(item.share)}（{item.count}）
                        </span>
                      );
                    })}
                  </div>
                  {platforms.length > 0 && (
                    <div className="ac-rank-list" aria-label="平台排行">
                      {platforms.map((platform) => (
                        <div key={platform.label} className="ac-rank-row">
                          <span>{platform.label}</span>
                          <strong>
                            {formatShare(platform.share)}（{platform.count}）
                          </strong>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <p className="ac-empty">暂无近 7 日终端统计</p>
              )}
            </article>

            <article className="ac-card">
              <div className="ac-card-hd">
                <div>
                  <h2 className="ac-card-title">核心服务状态（只读）</h2>
                  <p className="ac-card-sub">服务凭据、术语库与任务吞吐快照</p>
                </div>
                <Link to={`${adminPath}/services`} className="ac-link-btn">
                  去配置
                </Link>
              </div>
              <div className="ac-mini-list">
                <div className="ac-mini-row">
                  <span>免费上游池（{data.services.usable_providers} 个可用渠道）</span>
                  <span
                    className={`ac-badge ${
                      data.services.free_enabled ? (data.services.free_ready ? "ok" : "warn") : ""
                    }`}
                  >
                    {data.services.free_enabled
                      ? data.services.free_ready
                        ? "已开启且就绪"
                        : "已开启但未就绪"
                      : "未开启"}
                  </span>
                </div>
                <div className="ac-mini-row">
                  <span>版面解析（MinerU）</span>
                  <span className={`ac-badge ${data.services.mineru_configured ? "ok" : "err"}`}>
                    {data.services.mineru_configured ? "已就绪" : "未配置"}
                  </span>
                </div>
                <div className="ac-mini-row">
                  <span>公共术语库</span>
                  <span className={`ac-badge mono ${data.services.glossary_reloading ? "pen" : "ok"}`}>
                    {data.services.glossary_reloading
                      ? `重载中 · ${data.services.glossary_terms} 词条`
                      : `${data.services.glossary_terms.toLocaleString("zh-CN")} 词条`}
                  </span>
                </div>
                <div className="ac-mini-row">
                  <span>今日提交 / 完成</span>
                  <strong className="ac-mono">
                    {data.stats.today_submitted} / {data.stats.today_completed}
                  </strong>
                </div>
                <div className="ac-mini-row">
                  <span>近 7 日累计提交 / 完成</span>
                  <strong className="ac-mono">
                    {data.stats.period_submitted} / {data.stats.period_completed}
                  </strong>
                </div>
              </div>
            </article>
          </section>

          {data.stats.failed_24h > 0 && (
            <section className="ac-alert err" aria-label="近期失败提示">
              <div>
                <strong>最近 24 小时有 {data.stats.failed_24h} 条失败任务</strong>
                {firstFailure && (
                  <span>
                    ：{firstFailure.title || "未记录标题"}
                    {firstFailure.failure_reason ? `（${firstFailure.failure_reason}）` : ""}
                  </span>
                )}
              </div>
              <button
                type="button"
                className="ac-btn sm"
                onClick={() => void navigate(`${adminPath}/records?status=failed`)}
              >
                查看失败记录
              </button>
            </section>
          )}

          <section className="ac-card" aria-labelledby="active-jobs-heading">
            <div className="ac-card-hd">
              <div>
                <h2 className="ac-card-title" id="active-jobs-heading">
                  当前在途任务
                </h2>
                <p className="ac-card-sub">实时展示正在解析或翻译的文件名、页数、阶段、进度与提交时间</p>
              </div>
              <span className="ac-badge mono">{data.active_jobs.length} 个在途</span>
            </div>

            {data.active_jobs.length > 0 ? (
              <table className="ac-table">
                <thead>
                  <tr>
                    <th>文件与任务</th>
                    <th>页数</th>
                    <th>当前阶段</th>
                    <th>进度</th>
                    <th>方式与模型</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.active_jobs.map((job) => {
                    const submittedLabel = formatJobTime(job.created_at);
                    return (
                      <tr key={job.job_id}>
                        <td>
                          <div className="ac-active-job-name" title={job.filename || job.job_id}>
                            {job.filename || "未命名文件"}
                          </div>
                          <div className="ac-active-job-sub ac-mono">
                            #{job.job_id.slice(0, 8)}
                            {submittedLabel ? ` · ${submittedLabel}` : ""}
                          </div>
                        </td>
                        <td>{job.page_count} 页</td>
                        <td>{STAGE_LABELS[job.stage] ?? (job.stage || "等待调度")}</td>
                        <td className="ac-mono">
                          {job.progress <= 1 ? Math.round(job.progress * 100) : Math.round(job.progress)}%
                        </td>
                        <td>
                          <span className={`ac-badge ${job.channel_mode === "free" ? "pen" : ""}`}>
                            {job.channel_mode === "free" ? "免费体验" : "自备 Key"}
                          </span>{" "}
                          <span className="ac-mono">{job.model || "-"}</span>
                        </td>
                        <td>
                          <span className="ac-badge">{STATUS_LABELS[job.status] ?? job.status}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <p className="ac-empty">当前无在途任务。</p>
            )}
          </section>
        </>
      )}
    </>
  );
}
