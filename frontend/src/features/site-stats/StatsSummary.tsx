import { Link } from "react-router";

import { useSiteStats } from "@/entities/stats/model";
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

export function StatsSummary() {
  const { data, isPending } = useSiteStats();
  return (
    <section className="stats-summary" aria-labelledby="site-stats-title">
      <div className="stats-section-head">
        <h2 id="site-stats-title">本站累计使用</h2>
        <Link to="/stats">查看统计 →</Link>
      </div>
      {data ? (
        <StatsMetrics counts={data.total} />
      ) : (
        <p className="hint" role="status">
          {isPending ? "正在加载统计…" : "统计暂时不可用，稍后可在统计页重试。"}
        </p>
      )}
    </section>
  );
}
