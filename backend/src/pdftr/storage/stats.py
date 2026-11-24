"""与任务共用 SQLite 的匿名计数，调用者负责连接锁和事务。

按北京时间归日。每日汇总和累计访客哈希长期保留；访问去重明细保留 7 天窗口及一天余量。
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.stats import (
    PUBLIC_STATS_DAYS,
    ClientBreakdown,
    ClientBucket,
    StatsCounts,
    StatsDay,
    StatsOut,
    StatsPeriod,
)
from pdftr.domain.user_agent import ClientInfo

STAT_TIMEZONE = timezone(timedelta(hours=8))
SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics_daily (
    day TEXT PRIMARY KEY,
    page_views INTEGER NOT NULL DEFAULT 0,
    visitors INTEGER NOT NULL DEFAULT 0,
    jobs_submitted INTEGER NOT NULL DEFAULT 0,
    jobs_completed INTEGER NOT NULL DEFAULT 0,
    pages_submitted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS analytics_jobs (
    job_id TEXT PRIMARY KEY,
    completed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS analytics_visitors (
    visitor_hash TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS analytics_visit_days (
    day TEXT NOT NULL,
    visitor_hash TEXT NOT NULL,
    PRIMARY KEY (day, visitor_hash)
);
CREATE TABLE IF NOT EXISTS analytics_events (
    event_id TEXT PRIMARY KEY,
    day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analytics_events_day ON analytics_events(day);
CREATE TABLE IF NOT EXISTS analytics_client_daily (
    day TEXT NOT NULL,
    metric_type TEXT NOT NULL CHECK (metric_type IN ('visit', 'job')),
    device TEXT NOT NULL,
    os TEXT NOT NULL,
    browser TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, metric_type, device, os, browser)
);
CREATE TABLE IF NOT EXISTS analytics_failures (
    job_id TEXT PRIMARY KEY,
    failed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analytics_failures_time ON analytics_failures(failed_at);
"""


def day_of(now: datetime) -> str:
    return now.astimezone(STAT_TIMEZONE).date().isoformat()


class SqliteStats:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def initialize(self, now: datetime) -> None:
        created = self.db.execute(
            "INSERT OR IGNORE INTO analytics_meta (key, value) VALUES ('since', ?)", (now.isoformat(),)
        ).rowcount
        if created:
            # 升级前的任务不冒充新提交，也不将重试后的完成记入新统计。
            self.db.execute("INSERT OR IGNORE INTO analytics_jobs (job_id, completed) SELECT id, 1 FROM jobs")

    def _daily(self, day: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO analytics_daily (day) VALUES (?)", (day,))

    def _record_client(self, day: str, metric_type: Literal["visit", "job"], client: ClientInfo | None) -> None:
        if client is None:
            return
        self.db.execute(
            "INSERT INTO analytics_client_daily (day, metric_type, device, os, browser, count) "
            "VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(day, metric_type, device, os, browser) DO UPDATE SET count = count + 1",
            (day, metric_type, client.device, client.os, client.browser),
        )

    def job_saved(self, job: Job, now: datetime, *, client: ClientInfo | None = None) -> None:
        first = self.db.execute("INSERT OR IGNORE INTO analytics_jobs (job_id) VALUES (?)", (job.id,)).rowcount
        day = day_of(now)
        if first:
            self._daily(day)
            self.db.execute(
                "UPDATE analytics_daily SET jobs_submitted = jobs_submitted + 1, "
                "pages_submitted = pages_submitted + ? WHERE day = ?",
                (job.page_count, day),
            )
            # 缺省 UA 时默认计入 unknown 桶，避免统计口径遗漏。
            self._record_client(day, "job", client or ClientInfo())
        if job.status is JobStatus.DONE:
            completed = self.db.execute(
                "UPDATE analytics_jobs SET completed = 1 WHERE job_id = ? AND completed = 0", (job.id,)
            ).rowcount
            if completed:
                self._daily(day)
                self.db.execute("UPDATE analytics_daily SET jobs_completed = jobs_completed + 1 WHERE day = ?", (day,))
        elif job.status is JobStatus.FAILED:
            failed_at = now.astimezone(UTC).isoformat(timespec="microseconds")
            self.db.execute(
                "INSERT INTO analytics_failures (job_id, failed_at) VALUES (?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET failed_at = excluded.failed_at",
                (job.id, failed_at),
            )
            cutoff_48h = (now - timedelta(hours=48)).astimezone(UTC).isoformat(timespec="microseconds")
            self.db.execute("DELETE FROM analytics_failures WHERE failed_at < ?", (cutoff_48h,))

    def failed_count_since(self, now: datetime, *, hours: int = 24) -> int:
        """按独立匿名失败表查询近 N 小时内去重失败任务总数，不受审计开关和任务保留期影响。"""
        cutoff = (now - timedelta(hours=hours)).astimezone(UTC).isoformat(timespec="microseconds")
        row = self.db.execute(
            "SELECT COUNT(*) FROM analytics_failures WHERE failed_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row[0]) if row else 0

    def visit(self, event_id: str, visitor_id: str, now: datetime, *, client: ClientInfo | None = None) -> None:
        day = day_of(now)
        self._prune(day, now)
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO analytics_events (event_id, day) VALUES (?, ?)", (event_id, day)
        ).rowcount
        if not inserted:
            return
        visitor_hash = hashlib.sha256(visitor_id.encode()).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO analytics_visitors (visitor_hash) VALUES (?)", (visitor_hash,))
        first_today = self.db.execute(
            "INSERT OR IGNORE INTO analytics_visit_days (day, visitor_hash) VALUES (?, ?)", (day, visitor_hash)
        ).rowcount
        self._daily(day)
        self.db.execute(
            "UPDATE analytics_daily SET page_views = page_views + 1, visitors = visitors + ? WHERE day = ?",
            (first_today, day),
        )
        self._record_client(day, "visit", client)

    def client_breakdown(
        self, days: int, now: datetime, *, metric_type: Literal["job", "visit"] = "job"
    ) -> ClientBreakdown:
        if days < 1:
            raise ValueError("统计天数必须大于等于 1")
        today = now.astimezone(STAT_TIMEZONE).date()
        first = today - timedelta(days=days - 1)
        rows = self.db.execute(
            "SELECT device, os, browser, SUM(count) FROM analytics_client_daily "
            "WHERE metric_type = ? AND day >= ? AND day <= ? "
            "GROUP BY device, os, browser",
            (metric_type, first.isoformat(), today.isoformat()),
        ).fetchall()
        total = sum(int(row[3]) for row in rows)
        if total == 0:
            return ClientBreakdown()

        device_totals: dict[str, int] = {}
        platform_totals: dict[str, int] = {}
        for device, os_name, browser, count in rows:
            cnt = int(count)
            device_totals[str(device)] = device_totals.get(str(device), 0) + cnt
            platform_label = f"{os_name} · {browser}"
            platform_totals[platform_label] = platform_totals.get(platform_label, 0) + cnt

        devices = [
            ClientBucket(label=label, count=count, share=round(count / total, 4))
            for label, count in sorted(device_totals.items(), key=lambda item: (-item[1], item[0]))
        ]
        platforms = [
            ClientBucket(label=label, count=count, share=round(count / total, 4))
            for label, count in sorted(platform_totals.items(), key=lambda item: (-item[1], item[0]))[:4]
        ]
        return ClientBreakdown(total=total, devices=devices, platforms=platforms)

    def overview_counts(self, days: int, now: datetime) -> tuple[int, int, int, int]:
        if days < 1:
            raise ValueError("统计天数必须大于等于 1")
        today = now.astimezone(STAT_TIMEZONE).date()
        first = today - timedelta(days=days - 1)
        today_row = self.db.execute(
            "SELECT jobs_submitted, jobs_completed FROM analytics_daily WHERE day = ?",
            (today.isoformat(),),
        ).fetchone()
        period_row = self.db.execute(
            "SELECT COALESCE(SUM(jobs_submitted), 0), COALESCE(SUM(jobs_completed), 0) "
            "FROM analytics_daily WHERE day >= ? AND day <= ?",
            (first.isoformat(), today.isoformat()),
        ).fetchone()
        today_submitted = int(today_row[0]) if today_row else 0
        today_completed = int(today_row[1]) if today_row else 0
        period_submitted = int(period_row[0]) if period_row else 0
        period_completed = int(period_row[1]) if period_row else 0
        return today_submitted, today_completed, period_submitted, period_completed

    def _prune(self, day: str, now: datetime) -> None:
        previous = self.db.execute("SELECT value FROM analytics_meta WHERE key = 'last_visit_cleanup'").fetchone()
        if previous and previous[0] == day:
            return
        cutoff = (now.astimezone(STAT_TIMEZONE).date() - timedelta(days=7)).isoformat()
        self.db.execute("DELETE FROM analytics_events WHERE day < ?", (cutoff,))
        self.db.execute("DELETE FROM analytics_visit_days WHERE day < ?", (cutoff,))
        self.db.execute("INSERT OR REPLACE INTO analytics_meta (key, value) VALUES ('last_visit_cleanup', ?)", (day,))

    def summary(self, days: int, now: datetime) -> StatsOut:
        if days != PUBLIC_STATS_DAYS:
            raise ValueError("公开统计仅支持最近 7 天")
        today = now.astimezone(STAT_TIMEZONE).date()
        first = today - timedelta(days=days - 1)
        columns = "page_views, visitors, jobs_submitted, jobs_completed, pages_submitted"
        fields = tuple(StatsCounts.model_fields)
        since = datetime.fromisoformat(
            self.db.execute("SELECT value FROM analytics_meta WHERE key = 'since'").fetchone()[0]
        )
        since_day = since.astimezone(STAT_TIMEZONE).date()
        rows = self.db.execute(
            f"SELECT day, {columns} FROM analytics_daily WHERE day >= ? AND day <= ? ORDER BY day",
            (first.isoformat(), today.isoformat()),
        ).fetchall()
        by_day = {row[0]: dict(zip(fields, row[1:], strict=True)) for row in rows}
        daily = [
            StatsDay(
                date=(first + timedelta(days=offset)).isoformat(),
                available=first + timedelta(days=offset) >= since_day,
                **by_day.get((first + timedelta(days=offset)).isoformat(), {}),
            )
            for offset in range(days)
        ]
        counts = {field: sum(getattr(row, field) for row in daily) for field in fields}
        # 同一个浏览器跨日访问仍只算一次，不能把每日独立浏览器数相加。
        counts["visitors"] = self.db.execute(
            "SELECT COUNT(DISTINCT visitor_hash) FROM analytics_visit_days WHERE day >= ? AND day <= ?",
            (first.isoformat(), today.isoformat()),
        ).fetchone()[0]
        return StatsOut(
            since=since,
            generated_at=now,
            period=StatsPeriod(start_date=first.isoformat(), end_date=today.isoformat(), counts=StatsCounts(**counts)),
            today=StatsCounts(**by_day.get(today.isoformat(), {})),
            daily=daily,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
