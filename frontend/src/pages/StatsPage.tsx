import { useEffect, useState } from "react";
import { Link } from "react-router";

import { STATS_DAYS, useSiteStats } from "@/entities/stats/model";
import { StatsMetrics, STATS_METRICS } from "@/features/site-stats/StatsMetrics";
import type { SiteStats } from "@/shared/api/client";
import { ErrorCard } from "@/shared/feedback/ErrorCard";

type Metric = (typeof STATS_METRICS)[number]["key"];
const number = new Intl.NumberFormat("zh-CN");

function Trend({ daily, metric }: { daily: SiteStats["daily"]; metric: Metric }) {
  const max = Math.max(1, ...daily.map((day) => day[metric]));
  const width = 600;
  const chartHeight = 140;
  const step = width / Math.max(1, daily.length);
  const label = STATS_METRICS.find((item) => item.key === metric)!.label;
  return (
    <div className="stats-trend">
      <svg viewBox="0 0 600 180" role="img" aria-label={`最近 ${daily.length} 天${label}趋势，具体数值见下方每日明细`}>
        <line x1="0" x2="600" y1={chartHeight} y2={chartHeight} className="stats-axis" />
        {daily.map((day, i) => (
          <rect
            key={day.date}
            x={i * step + step * 0.18}
            y={chartHeight - (day[metric] / max) * (chartHeight - 12)}
            width={step * 0.64}
            height={(day[metric] / max) * (chartHeight - 12)}
            rx="2"
          >
            <title>{`${day.date}：${label} ${number.format(day[metric])}`}</title>
          </rect>
        ))}
        {daily
          .filter((_day, i) => i === 0 || i === daily.length - 1 || i === Math.floor(daily.length / 2))
          .map((day) => {
            const index = daily.indexOf(day);
            return (
              <text
                key={day.date}
                x={index === 0 ? 0 : index === daily.length - 1 ? width : width / 2}
                y="166"
                textAnchor={index === 0 ? "start" : index === daily.length - 1 ? "end" : "middle"}
              >
                {day.date.slice(5)}
              </text>
            );
          })}
      </svg>
      {daily.every((day) => day[metric] === 0) && <p className="hint">这段时间还没有{label}记录。</p>}
    </div>
  );
}

export function StatsPage() {
  const { data, error, isFetching, refetch } = useSiteStats();
  const [metric, setMetric] = useState<Metric>("page_views");
  useEffect(() => {
    document.title = "访问与使用统计";
  }, []);
  return (
    <main className="stats-page">
      <Link to="/" className="back">
        返回首页
      </Link>
      <div className="stats-page-head">
        <div>
          <h1>访问与使用统计</h1>
          <p className="hint">展示最近 {STATS_DAYS} 天的访问和翻译使用情况，所有日期均按北京时间统计。</p>
        </div>
        <button type="button" className="quiet" disabled={isFetching} onClick={() => void refetch()}>
          {isFetching ? "刷新中…" : "刷新数据"}
        </button>
      </div>
      {!data && error ? (
        <ErrorCard
          title="统计暂时不可用"
          message="未能获取统计数据，请稍后重试。"
          actionLabel="重新加载"
          onAction={() => void refetch()}
        />
      ) : !data ? (
        <p role="status" className="hint">
          正在加载统计…
        </p>
      ) : (
        <>
          <section className="stats-card" aria-labelledby="stats-today">
            <div className="stats-section-head">
              <h2 id="stats-today">今日</h2>
              <span className="hint">{data.daily.at(-1)?.date}</span>
            </div>
            <StatsMetrics counts={data.today} />
          </section>
          <section className="stats-card" aria-labelledby="stats-trend-title">
            <div className="stats-section-head">
              <h2 id="stats-trend-title">最近 {STATS_DAYS} 天</h2>
              <label className="stats-metric-select">
                <span className="hint">查看指标</span>
                <select value={metric} onChange={(event) => setMetric(event.target.value as Metric)}>
                  {STATS_METRICS.map(({ key, label }) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <Trend daily={data.daily} metric={metric} />
            <details className="stats-details">
              <summary>每日明细</summary>
              <div className="stats-table-scroll" tabIndex={0} role="region" aria-label="可横向滚动的每日统计">
                <table>
                  <caption className="visually-hidden">最近 {STATS_DAYS} 天每日访问与使用统计</caption>
                  <thead>
                    <tr>
                      <th scope="col">日期</th>
                      {STATS_METRICS.map(({ key, label }) => (
                        <th key={key} scope="col">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {[...data.daily].reverse().map((day) => (
                      <tr key={day.date}>
                        <th scope="row">{day.date}</th>
                        {STATS_METRICS.map(({ key }) => (
                          <td key={key} className="num">
                            {number.format(day[key])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </section>
          {error && (
            <p className="hint" role="status">
              本次刷新失败，当前展示上一次成功获取的数据。
            </p>
          )}
          <aside className="stats-note">
            <h2>统计口径</h2>
            <ul>
              <li>打开或切换一个页面记一次访问；任务进度轮询、健康检查和静态文件请求不计入。</li>
              <li>匿名访客按浏览器随机标识去重，不等同于实际人数；清除浏览器数据或更换浏览器会重新计数。</li>
              <li>任务成功提交时统计篇数和完整 PDF 的上传页数，每个任务首次完成时记一次完成；重试不会重复计入。</li>
              <li>页面仅展示最近 {STATS_DAYS} 天，历史汇总数据长期保留；任务到期或删除不会清除使用统计。</li>
              <li>统计不保存论文内容、文件名、API Key 或原始 IP，也不向第三方发送统计数据。</li>
            </ul>
          </aside>
        </>
      )}
    </main>
  );
}
