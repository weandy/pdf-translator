"""与任务共用 SQLite 的匿名计数，调用者负责连接锁和事务。

按北京时间归日。每日汇总和累计访客哈希长期保留；访问事件与每日访客去重明细只保留 7 天。
"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.stats import StatsCounts, StatsDay, StatsOut

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

    def job_saved(self, job: Job, now: datetime) -> None:
        first = self.db.execute("INSERT OR IGNORE INTO analytics_jobs (job_id) VALUES (?)", (job.id,)).rowcount
        day = day_of(now)
        if first:
            self._daily(day)
            self.db.execute(
                "UPDATE analytics_daily SET jobs_submitted = jobs_submitted + 1, "
                "pages_submitted = pages_submitted + ? WHERE day = ?",
                (job.page_count, day),
            )
        if job.status is JobStatus.DONE:
            completed = self.db.execute(
                "UPDATE analytics_jobs SET completed = 1 WHERE job_id = ? AND completed = 0", (job.id,)
            ).rowcount
            if completed:
                self._daily(day)
                self.db.execute("UPDATE analytics_daily SET jobs_completed = jobs_completed + 1 WHERE day = ?", (day,))

    def visit(self, event_id: str, visitor_id: str, now: datetime) -> None:
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

    def _prune(self, day: str, now: datetime) -> None:
        previous = self.db.execute("SELECT value FROM analytics_meta WHERE key = 'last_visit_cleanup'").fetchone()
        if previous and previous[0] == day:
            return
        cutoff = (now.astimezone(STAT_TIMEZONE).date() - timedelta(days=7)).isoformat()
        self.db.execute("DELETE FROM analytics_events WHERE day < ?", (cutoff,))
        self.db.execute("DELETE FROM analytics_visit_days WHERE day < ?", (cutoff,))
        self.db.execute("INSERT OR REPLACE INTO analytics_meta (key, value) VALUES ('last_visit_cleanup', ?)", (day,))

    def summary(self, days: int, now: datetime) -> StatsOut:
        today = now.astimezone(STAT_TIMEZONE).date()
        first = today - timedelta(days=days - 1)
        columns = "page_views, visitors, jobs_submitted, jobs_completed, pages_submitted"
        fields = tuple(StatsCounts.model_fields)
        totals = self.db.execute(
            "SELECT COALESCE(SUM(page_views), 0), "
            "(SELECT COUNT(*) FROM analytics_visitors), "
            "COALESCE(SUM(jobs_submitted), 0), COALESCE(SUM(jobs_completed), 0), "
            "COALESCE(SUM(pages_submitted), 0) FROM analytics_daily"
        ).fetchone()
        rows = self.db.execute(
            f"SELECT day, {columns} FROM analytics_daily WHERE day >= ? AND day <= ? ORDER BY day",
            (first.isoformat(), today.isoformat()),
        ).fetchall()
        by_day = {row[0]: dict(zip(fields, row[1:], strict=True)) for row in rows}
        daily = [
            StatsDay(
                date=(first + timedelta(days=offset)).isoformat(),
                **by_day.get((first + timedelta(days=offset)).isoformat(), {}),
            )
            for offset in range(days)
        ]
        since = self.db.execute("SELECT value FROM analytics_meta WHERE key = 'since'").fetchone()[0]
        return StatsOut(
            since=datetime.fromisoformat(since),
            total=StatsCounts(**dict(zip(fields, totals, strict=True))),
            today=StatsCounts(**by_day.get(today.isoformat(), {})),
            daily=daily,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)
