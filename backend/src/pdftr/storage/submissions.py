"""独立于任务 TTL 的提交账本；调用者负责数据库锁、事务与采集开关。"""

import sqlite3
from datetime import UTC, datetime

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.submissions import (
    SubmissionContext,
    SubmissionPage,
    SubmissionRecord,
    SubmissionSearch,
    clean_title,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_audit (
    job_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_source TEXT NOT NULL CHECK (title_source IN ('parsed', 'filename', 'unknown')),
    submitted_at TEXT NOT NULL,
    submitter_ip TEXT,
    ip_source TEXT NOT NULL CHECK (ip_source IN ('trusted_proxy', 'direct', 'unknown')),
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    first_completed_at TEXT,
    updated_at TEXT NOT NULL,
    record_source TEXT NOT NULL CHECK (record_source IN ('live', 'backfill')),
    notice_version TEXT
);
CREATE INDEX IF NOT EXISTS submission_audit_time ON submission_audit (submitted_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS submission_audit_ip ON submission_audit (submitter_ip, submitted_at DESC, job_id DESC);
"""
_COLUMNS = (
    "job_id, title, title_source, submitted_at, submitter_ip, ip_source, page_count, status, "
    "first_completed_at, updated_at, record_source, notice_version"
)


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class SqliteSubmissions:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def create(self, job: Job, context: SubmissionContext | None, now: datetime, *, backfill: bool = False) -> bool:
        context = context or SubmissionContext()
        title = clean_title(job.filename, filename=True)
        # 补录没有可靠的首次完成时间，不能用最后更新时间猜测。
        completed = timestamp(now) if job.status is JobStatus.DONE and not backfill else None
        return bool(
            self.db.execute(
                f"INSERT OR IGNORE INTO submission_audit ({_COLUMNS}) VALUES ({', '.join('?' for _ in range(12))})",
                (
                    job.id,
                    title or "未知标题",
                    "filename" if title else "unknown",
                    timestamp(job.created_at),
                    None if backfill else context.submitter_ip,
                    "unknown" if backfill else context.ip_source,
                    job.page_count,
                    job.status,
                    completed,
                    timestamp(now),
                    "backfill" if backfill else "live",
                    None if backfill else context.notice_version,
                ),
            ).rowcount
        )

    def job_saved(self, job: Job, now: datetime, *, first_completion: bool) -> None:
        self.db.execute(
            "UPDATE submission_audit SET status = ?, updated_at = ?, "
            "first_completed_at = COALESCE(first_completed_at, ?) WHERE job_id = ?",
            (job.status, timestamp(now), timestamp(now) if first_completion else None, job.id),
        )

    def update_title(self, job_id: str, title: str, now: datetime) -> None:
        if title := clean_title(title):
            self.db.execute(
                "UPDATE submission_audit SET title = ?, title_source = 'parsed', updated_at = ? WHERE job_id = ?",
                (title, timestamp(now), job_id),
            )

    def search(self, query: SubmissionSearch, now: datetime) -> SubmissionPage:
        conditions: list[str] = []
        params: list[str | int] = []
        if query.title_query:
            conditions.append("title LIKE ? ESCAPE '\\'")
            escaped = query.title_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if query.ip:
            conditions.append("submitter_ip = ?")
            params.append(query.ip)
        start, end = query.utc_bounds()
        if start:
            conditions.append("submitted_at >= ?")
            params.append(timestamp(start))
        if end:
            conditions.append("submitted_at < ?")
            params.append(timestamp(end))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM submission_audit{where}", params).fetchone()[0]
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM submission_audit{where} ORDER BY submitted_at DESC, job_id DESC LIMIT ? OFFSET ?",
            [*params, query.page_size, (query.page - 1) * query.page_size],
        ).fetchall()
        fields = tuple(SubmissionRecord.model_fields)
        return SubmissionPage(
            generated_at=now,
            total=total,
            page=query.page,
            page_size=query.page_size,
            items=[SubmissionRecord.model_validate(dict(zip(fields, row, strict=True))) for row in rows],
        )
