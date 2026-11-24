"""任务与段落的 SQLite 仓储（与译文缓存共用 data/app.db）。

段落整体存为 JSON，另存状态列便于查询；数据量为每篇论文几百行，足够简单可靠。
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pdftr.domain.ir import Revision, Segment, SegmentStatus
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.stats import AdminStatsSnapshot, OverviewFailureSummary, StatsOut
from pdftr.domain.submissions import (
    SubmissionContext,
    SubmissionPage,
    SubmissionSearch,
    clean_title,
    failure_details,
)
from pdftr.domain.user_agent import parse_user_agent
from pdftr.storage.connection import connect, retry_busy, write_tx
from pdftr.storage.stats import SCHEMA as STATS_SCHEMA
from pdftr.storage.stats import SqliteStats, utc_now
from pdftr.storage.submissions import SCHEMA as SUBMISSIONS_SCHEMA
from pdftr.storage.submissions import SqliteSubmissions, backfill_failure_details
from pdftr.storage.submissions import ensure_columns as ensure_submission_columns

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_job ON segments (job_id, idx);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_job ON revisions (job_id, id);
CREATE TABLE IF NOT EXISTS artifact_cleanup (
    job_id TEXT PRIMARY KEY,
    queued_at TEXT NOT NULL
);
"""


class SqliteStore:
    """同一个连接上的任务仓储与段落仓储。"""

    def __init__(self, path: Path, *, submission_audit_enabled: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = connect(path)
        self._submission_audit_enabled = submission_audit_enabled
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    @retry_busy
    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        # 覆盖主库逻辑删除的单元格；WAL 和既有备份仍需独立的保留策略。
        self._db.execute("PRAGMA secure_delete=ON")
        self._stats = SqliteStats(self._db)
        self._submissions = SqliteSubmissions(self._db)
        with self._lock, write_tx(self._db):
            # 固定的建表语句逐条执行，避免 executescript 隐式提交。
            for statement in (_SCHEMA + STATS_SCHEMA + SUBMISSIONS_SCHEMA).split(";"):
                if statement.strip():
                    self._db.execute(statement)
            ensure_submission_columns(self._db)
            now = utc_now()
            backfill_failure_details(self._db, now)
            self._stats.initialize(now)

    # ---- 任务 ----
    @retry_busy
    def create_job(self, job: Job, context: SubmissionContext | None = None) -> None:
        """首次成功接收的任务、公开统计、可选账本必须原子提交。"""
        now = utc_now()
        job.updated_at = now
        client = parse_user_agent(context.user_agent) if context is not None and context.user_agent else None
        with self._lock, write_tx(self._db):
            self._db.execute(
                "INSERT INTO jobs (id, status, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
            if client is not None:
                self._stats.job_saved(job, now, client=client)
            else:
                self._stats.job_saved(job, now)
            if self._submission_audit_enabled:
                self._submissions.create(job, context, now)

    @retry_busy
    def save_job(self, job: Job, context: SubmissionContext | None = None) -> None:
        now = utc_now()
        job.updated_at = now
        client = parse_user_agent(context.user_agent) if context is not None and context.user_agent else None
        with self._lock, write_tx(self._db):
            completed = self._db.execute("SELECT completed FROM analytics_jobs WHERE job_id = ?", (job.id,)).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (id, status, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
            if client is not None:
                self._stats.job_saved(job, now, client=client)
            else:
                self._stats.job_saved(job, now)
            if self._submission_audit_enabled:
                # 统计去重状态在停采时也保留，避免恢复采集后伪造真正的首次完成时间。
                first_completion = job.status is JobStatus.DONE and not (completed and completed[0])
                self._submissions.job_saved(job, now, first_completion=first_completion)

    @retry_busy
    def record_visit(self, event_id: str, visitor_id: str, *, user_agent: str | None = None) -> None:
        client = parse_user_agent(user_agent)
        with self._lock, write_tx(self._db):
            self._stats.visit(event_id, visitor_id, utc_now(), client=client)

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        # 连接锁只能隔离本进程，显式读事务还防止其他连接在多个 SELECT 之间提交。
        self._db.execute("BEGIN")
        try:
            yield
        finally:
            self._db.rollback()

    @retry_busy
    def stats(self, days: int = 7) -> StatsOut:
        with self._lock, self._read_snapshot():
            return self._stats.summary(days, utc_now())

    def _recent_failures_from_jobs(
        self, now: datetime, *, hours: int = 24, limit: int = 5
    ) -> tuple[int, list[OverviewFailureSummary]]:
        cutoff = now - timedelta(hours=hours)
        rows = self._db.execute("SELECT data FROM jobs WHERE status = ?", (JobStatus.FAILED,)).fetchall()
        matched: list[tuple[datetime, Job]] = []
        for row in rows:
            try:
                job = Job.model_validate_json(row[0])
            except (TypeError, ValueError):
                continue
            failed_at = job.updated_at or job.created_at
            if failed_at >= cutoff:
                matched.append((failed_at, job))
        matched.sort(key=lambda item: (item[0], item[1].id), reverse=True)
        summaries: list[OverviewFailureSummary] = []
        for failed_at, job in matched[:limit]:
            _, reason, retryable, _ = failure_details(job, failed_at)
            title = clean_title(job.filename, filename=True) or "未知标题"
            summaries.append(
                OverviewFailureSummary(
                    job_id=job.id,
                    title=title,
                    submitted_at=job.created_at,
                    channel_mode=job.channel_mode,
                    failure_reason=reason or "任务处理失败，可重试；如持续失败请检查服务日志",
                    retryable=retryable,
                )
            )
        return len(matched), summaries

    @retry_busy
    def admin_overview_stats(self, days: int = 7) -> AdminStatsSnapshot:
        with self._lock, self._read_snapshot():
            now = utc_now()
            today_submitted, today_completed, period_submitted, period_completed = self._stats.overview_counts(
                days, now
            )
            failed_24h = self._stats.failed_count_since(now, hours=24)
            if self._submission_audit_enabled:
                _, raw_failures = self._submissions.recent_failures(now, hours=24, limit=5)
                recent_failures = [
                    OverviewFailureSummary(
                        job_id=rec.job_id,
                        title=rec.title,
                        submitted_at=rec.submitted_at,
                        channel_mode=rec.channel_mode,
                        failure_reason=rec.failure_reason or "任务处理失败，可重试；如持续失败请检查服务日志",
                        retryable=rec.retryable,
                    )
                    for rec in raw_failures
                ]
            else:
                _, recent_failures = self._recent_failures_from_jobs(now, hours=24, limit=5)
            breakdown = self._stats.client_breakdown(days, now, metric_type="job")
            breakdown_source: Literal["job", "visit"] = "job"
            if breakdown.total == 0:
                visit_breakdown = self._stats.client_breakdown(days, now, metric_type="visit")
                if visit_breakdown.total > 0:
                    breakdown = visit_breakdown
                    breakdown_source = "visit"
            return AdminStatsSnapshot(
                today_submitted=today_submitted,
                today_completed=today_completed,
                period_submitted=period_submitted,
                period_completed=period_completed,
                failed_24h=failed_24h,
                recent_failures=recent_failures,
                client_breakdown=breakdown,
                client_breakdown_source=breakdown_source,
            )

    @retry_busy
    def search_submissions(self, query: SubmissionSearch) -> SubmissionPage:
        with self._lock, self._read_snapshot():
            return self._submissions.search(query, utc_now())

    @retry_busy
    def update_submission_title(self, job_id: str, title: str) -> None:
        if self._submission_audit_enabled:
            with self._lock, write_tx(self._db):
                self._submissions.update_title(job_id, title, utc_now())

    @retry_busy
    def backfill_submissions(self, titles: dict[str, str] | None = None) -> int:
        """显式历史迁移入口；不自动运行，不修改原统计或猜测 IP、首次完成时间。"""
        if not self._submission_audit_enabled:
            return 0
        with self._lock, write_tx(self._db):
            now = utc_now()
            rows = self._db.execute("SELECT data FROM jobs").fetchall()
            inserted = 0
            for row in rows:
                job = Job.model_validate_json(row[0])
                if self._submissions.create(job, None, now, backfill=True):
                    inserted += 1
                    if titles and job.id in titles:
                        self._submissions.update_title(job.id, titles[job.id], now)
            return inserted

    @retry_busy
    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    @retry_busy
    def delete_job(self, job_id: str) -> None:
        with self._lock, write_tx(self._db):
            self._queue_artifact_cleanup(job_id)
            self._db.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._db.execute("DELETE FROM revisions WHERE job_id = ?", (job_id,))
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def _queue_artifact_cleanup(self, job_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO artifact_cleanup (job_id, queued_at) VALUES (?, ?)",
            (job_id, utc_now().isoformat()),
        )

    @retry_busy
    def queue_artifact_cleanup(self, job_id: str) -> None:
        with self._lock, write_tx(self._db):
            self._queue_artifact_cleanup(job_id)

    @retry_busy
    def pending_artifact_cleanup(self) -> list[str]:
        with self._lock:
            return [
                row[0] for row in self._db.execute("SELECT job_id FROM artifact_cleanup ORDER BY queued_at, job_id")
            ]

    @retry_busy
    def complete_artifact_cleanup(self, job_id: str) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute("DELETE FROM artifact_cleanup WHERE job_id = ?", (job_id,))

    @retry_busy
    def checkpoint_after_purge(self) -> None:
        # PASSIVE 不强等活动读者，不做耗时、需要额外磁盘空间的在线 VACUUM。
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @retry_busy
    def all_jobs(self) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs").fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    @retry_busy
    def jobs_with_status(self, status: JobStatus) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs WHERE status = ?", (status,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    # ---- 段落 ----
    @retry_busy
    def replace_segments(self, job_id: str, segments: list[Segment]) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._db.executemany(
                "INSERT INTO segments (id, job_id, idx, status, data) VALUES (?, ?, ?, ?, ?)",
                [(s.id, job_id, i, s.status, s.model_dump_json()) for i, s in enumerate(segments)],
            )

    @retry_busy
    def save_segments(self, segments: list[Segment]) -> None:
        with self._lock, write_tx(self._db):
            self._db.executemany(
                "UPDATE segments SET status = ?, data = ? WHERE id = ?",
                [(s.status, s.model_dump_json(), s.id) for s in segments],
            )

    @retry_busy
    def segments(self, job_id: str) -> list[Segment]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM segments WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall()
        return [Segment.model_validate_json(r[0]) for r in rows]

    @retry_busy
    def segment(self, segment_id: str) -> Segment | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM segments WHERE id = ?", (segment_id,)).fetchone()
        return Segment.model_validate_json(row[0]) if row else None

    @retry_busy
    def count_segments(self, job_id: str, status: SegmentStatus) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM segments WHERE job_id = ? AND status = ?", (job_id, status)
            ).fetchone()
        return int(row[0])

    # ---- 修改记录 ----
    @retry_busy
    def add_revision(self, rev: Revision) -> Revision:
        with self._lock, write_tx(self._db):
            cur = self._db.execute(
                "INSERT INTO revisions (job_id, segment_id, data) VALUES (?, ?, '')", (rev.job_id, rev.segment_id)
            )
            rev = rev.model_copy(update={"id": cur.lastrowid})
            self._db.execute("UPDATE revisions SET data = ? WHERE id = ?", (rev.model_dump_json(), rev.id))
        return rev

    @retry_busy
    def save_revisions(self, revs: list[Revision]) -> None:
        with self._lock, write_tx(self._db):
            self._db.executemany(
                "UPDATE revisions SET data = ? WHERE id = ?", [(r.model_dump_json(), r.id) for r in revs]
            )

    @retry_busy
    def revisions(self, job_id: str) -> list[Revision]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM revisions WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        return [Revision.model_validate_json(r[0]) for r in rows]

    @retry_busy
    def revision(self, revision_id: int) -> Revision | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        return Revision.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
