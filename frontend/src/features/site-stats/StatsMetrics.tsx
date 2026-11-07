import type { SiteStats } from "@/shared/api/client";

export const STATS_METRICS = [
  { key: "page_views", label: "页面访问", unit: "次" },
  { key: "visitors", label: "匿名访客", unit: "个浏览器" },
  { key: "jobs_submitted", label: "提交任务", unit: "篇" },
  { key: "jobs_completed", label: "完成翻译", unit: "篇" },
  { key: "pages_submitted", label: "上传页数", unit: "页" },
] as const;

const number = new Intl.NumberFormat("zh-CN");

export function StatsMetrics({ counts }: { counts: SiteStats["total"] }) {
  return (
    <dl className="stats-metrics">
      {STATS_METRICS.map(({ key, label, unit }) => (
        <div key={key}>
          <dt>{label}</dt>
          <dd className="num">
            {number.format(counts[key])}
            <small>{unit}</small>
          </dd>
        </div>
      ))}
    </dl>
  );
}
