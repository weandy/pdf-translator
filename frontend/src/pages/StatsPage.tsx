import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { STATS_DAYS, useSiteStats } from "@/entities/stats/model";
import { StatsMetrics, STATS_METRICS } from "@/features/site-stats/StatsMetrics";
import type { SiteStats } from "@/shared/api/client";
import { ErrorCard } from "@/shared/feedback/ErrorCard";

type Metric = (typeof STATS_METRICS)[number]["key"];
const number = new Intl.NumberFormat("zh-CN");
const compact = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
const updatedTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function Trend({ daily, metric }: { daily: SiteStats["daily"]; metric: Metric }) {
  const max = Math.max(1, ...daily.map((day) => day[metric]));
  const width = 600;
  const chartHeight = 140;
  const step = width / Math.max(1, daily.length);
  const label = STATS_METRICS.find((item) => item.key === metric)!.label;
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const selected = daily.find((day) => day.date === selectedDate) ?? daily.at(-1);
  return (
    <div className="stats-trend">
      <svg viewBox="0 0 600 180" aria-hidden="true" focusable="false">
        <line x1="0" x2="600" y1={chartHeight} y2={chartHeight} className="stats-axis" />
        {daily.map((day, i) => (
          <g key={day.date} className={day.date === selected?.date ? "stats-day-selected" : undefined}>
            <rect
              x={i * step + step * 0.18}
              y={chartHeight - (day[metric] / max) * (chartHeight - 12)}
              width={step * 0.64}
              height={(day[metric] / max) * (chartHeight - 12)}
              rx="2"
            />
            <text
              x={(i + 0.5) * step}
              y={Math.max(12, chartHeight - (day[metric] / max) * (chartHeight - 12) - 6)}
              textAnchor="middle"
            >
              {day.available ? compact.format(day[metric]) : "—"}
            </text>
            <text x={(i + 0.5) * step} y="166" textAnchor="middle">
              {day.date.slice(5)}
            </text>
          </g>
        ))}
      </svg>
      <div className="stats-day-buttons" aria-label="选择统计日期">
        {daily.map((day, index) => (
          <button
            key={day.date}
            type="button"
            ref={(element) => {
              buttons.current[index] = element;
            }}
            aria-pressed={day.date === selected?.date}
            aria-label={`${day.date}，${label} ${day.available ? number.format(day[metric]) : "尚未启用"}`}
            onClick={() => setSelectedDate(day.date)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
              event.preventDefault();
              const next = (index + (event.key === "ArrowRight" ? 1 : daily.length - 1)) % daily.length;
              setSelectedDate(daily[next]!.date);
              buttons.current[next]?.focus();
            }}
          >
            <span className="visually-hidden">选择 {day.date}</span>
          </button>
        ))}
      </div>
      {selected && (
        <p className="stats-selection" aria-live="polite">
          {selected.date} · {label}：{selected.available ? number.format(selected[metric]) : "尚未启用"}
        </p>
      )}
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
          {data && (
            <p className="hint">
              {data.period.start_date} 至 {data.period.end_date} · 数据更新于{" "}
              {updatedTime.format(new Date(data.generated_at))}
            </p>
          )}
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
          <section className="stats-card" aria-labelledby="stats-period">
            <div className="stats-section-head">
              <h2 id="stats-period">最近 {STATS_DAYS} 天汇总</h2>
              <span className="hint">含今天</span>
            </div>
            <StatsMetrics counts={data.period.counts} />
          </section>
          <section className="stats-card" aria-labelledby="stats-today">
            <div className="stats-section-head">
              <h2 id="stats-today">今日</h2>
              <span className="hint">{data.period.end_date} · 持续累计</span>
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
                            {day.available ? number.format(day[key]) : "尚未启用"}
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
              <li>
                访问浏览器按随机标识去重，不等同于实际人数或浏览器种类；清除数据、无痕浏览或更换设备可能重新计数。最近 7
                天跨日去重，不将每日浏览器数相加。
              </li>
              <li>任务成功提交时统计篇数和完整 PDF 的上传页数，每个任务首次完成时记一次完成；重试不会重复计入。</li>
              <li>页面仅展示最近 {STATS_DAYS} 天，历史汇总数据长期保留；任务到期或删除不会清除使用统计。</li>
              <li>
                今天及之前 6
                天按北京时间自然日统计，今天尚未结束；完成任务可能在更早日期提交，不能用完成数除以提交数作为成功率。统计自{" "}
                {new Date(data.since).toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" })}{" "}
                启用，启用当天可能只包含部分时段。
              </li>
              <li>统计不保存论文内容、文件名、API Key 或原始 IP，也不向第三方发送统计数据。</li>
            </ul>
            <Link to="/admin/login">管理登录</Link>
          </aside>
        </>
      )}
    </main>
  );
}
