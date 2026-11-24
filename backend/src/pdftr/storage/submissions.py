"""独立于任务 TTL 的提交账本；调用者负责数据库锁、事务与采集开关。"""

import sqlite3
from datetime import UTC, datetime, timedelta

from pdftr.domain.job import Job, JobStatus
from pdftr.domain.submissions import (
    SubmissionContext,
    SubmissionPage,
    SubmissionRecord,
    SubmissionSearch,
    clean_title,
    failure_details,
)
from pdftr.domain.user_agent import parse_user_agent

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
    notice_version TEXT,
    current_stage TEXT,
    failure_code TEXT,
    failure_reason TEXT,
    retryable INTEGER,
    failure_updated_at TEXT,
    channel_mode TEXT,
    model_name TEXT,
    fallback_used INTEGER,
    user_agent TEXT,
    client_device TEXT,
    client_os TEXT,
    client_browser TEXT
);
CREATE INDEX IF NOT EXISTS submission_audit_time ON submission_audit (submitted_at DESC, job_id DESC);
CREATE INDEX IF NOT EXISTS submission_audit_ip ON submission_audit (submitter_ip, submitted_at DESC, job_id DESC);
"""
_COLUMNS = (
    "job_id, title, title_source, submitted_at, submitter_ip, ip_source, page_count, status, "
    "first_completed_at, updated_at, record_source, notice_version, current_stage, failure_code, "
    "failure_reason, retryable, failure_updated_at, channel_mode, model_name, fallback_used, "
    "user_agent, client_device, client_os, client_browser"
)

_MIGRATION_COLUMNS = {
    "current_stage": "TEXT",
    "failure_code": "TEXT",
    "failure_reason": "TEXT",
    "retryable": "INTEGER",
    "failure_updated_at": "TEXT",
    "channel_mode": "TEXT",
    "model_name": "TEXT",
    "fallback_used": "INTEGER",
    "user_agent": "TEXT",
    "client_device": "TEXT",
    "client_os": "TEXT",
    "client_browser": "TEXT",
}


def ensure_columns(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(submission_audit)")}
    for name, kind in _MIGRATION_COLUMNS.items():
        if name not in existing:
            db.execute(f"ALTER TABLE submission_audit ADD COLUMN {name} {kind}")


def backfill_failure_details(db: sqlite3.Connection, now: datetime) -> None:
    """为升级前仍存在的失败任务补上安全摘要；已清理任务保持未知。"""
    rows = db.execute(
        "SELECT a.job_id, j.data FROM submission_audit AS a "
        "JOIN jobs AS j ON j.id = a.job_id "
        "WHERE a.status = 'failed' AND a.failure_reason IS NULL"
    ).fetchall()
    for job_id, data in rows:
        try:
            job = Job.model_validate_json(data)
        except (TypeError, ValueError):
            continue
        code, reason, retryable, failure_at = failure_details(job, job.updated_at or now)
        if reason is None:
            continue
        db.execute(
            "UPDATE submission_audit SET current_stage = ?, failure_code = ?, failure_reason = ?, "
            "retryable = ?, failure_updated_at = ? WHERE job_id = ? AND failure_reason IS NULL",
            (job.stage, code, reason, None if retryable is None else int(retryable), failure_at, job_id),
        )


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class SqliteSubmissions:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @staticmethod
    def _row_to_record(row: tuple[object, ...]) -> SubmissionRecord:
        fields = tuple(SubmissionRecord.model_fields)
        values = dict(zip(fields, row, strict=True))
        if values["retryable"] is not None:
            values["retryable"] = bool(values["retryable"])
        if values["fallback_used"] is not None:
            values["fallback_used"] = bool(values["fallback_used"])
        return SubmissionRecord.model_validate(values)

    def create(self, job: Job, context: SubmissionContext | None, now: datetime, *, backfill: bool = False) -> bool:
        context = context or SubmissionContext()
        title = clean_title(job.filename, filename=True)
        # 补录没有可靠的首次完成时间，不能用最后更新时间猜测。
        completed = timestamp(now) if job.status is JobStatus.DONE and not backfill else None
        code, reason, retryable, failure_at = failure_details(job, now)
        if backfill:
            channel_mode = None
            model_name = None
            fallback_used = None
            user_agent = None
            client_device = None
            client_os = None
            client_browser = None
        else:
            channel_mode = context.channel_mode
            model_name = (job.model or context.model_name).strip() or None
            fallback_used = int(bool(getattr(job, "fallback_used", False)))
            client = parse_user_agent(context.user_agent)
            user_agent = client.raw or None
            client_device = client.device
            client_os = client.os
            client_browser = client.browser
        return bool(
            self.db.execute(
                f"INSERT OR IGNORE INTO submission_audit ({_COLUMNS}) VALUES ({', '.join('?' for _ in range(24))})",
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
                    job.stage,
                    code,
                    reason,
                    None if retryable is None else int(retryable),
                    failure_at,
                    channel_mode,
                    model_name,
                    fallback_used,
                    user_agent,
                    client_device,
                    client_os,
                    client_browser,
                ),
            ).rowcount
        )

    def job_saved(self, job: Job, now: datetime, *, first_completion: bool = False) -> None:
        code, reason, retryable, failure_at = failure_details(job, now)
        model_name = job.model.strip() or None
        fallback_flag = 1 if getattr(job, "fallback_used", False) else 0
        self.db.execute(
            "UPDATE submission_audit SET status = ?, updated_at = ?, "
            "first_completed_at = COALESCE(first_completed_at, ?), current_stage = ?, failure_code = ?, "
            "failure_reason = ?, retryable = ?, failure_updated_at = ?, "
            "model_name = COALESCE(?, model_name), "
            "fallback_used = CASE WHEN ? = 1 THEN 1 ELSE fallback_used END "
            "WHERE job_id = ?",
            (
                job.status,
                timestamp(now),
                timestamp(now) if first_completion else None,
                job.stage,
                code,
                reason,
                None if retryable is None else int(retryable),
                failure_at,
                model_name,
                fallback_flag,
                job.id,
            ),
        )

    def sync(self, job: Job, now: datetime, *, first_completion: bool = False) -> None:
        self.job_saved(job, now, first_completion=first_completion)

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
        if query.channel_mode is not None:
            conditions.append("channel_mode = ?")
            params.append(query.channel_mode)
        if query.status is not None:
            conditions.append("status = ?")
            params.append(query.status)
        if query.client_device is not None:
            conditions.append("client_device = ?")
            params.append(query.client_device)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM submission_audit{where}", params).fetchone()[0]
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM submission_audit{where} ORDER BY submitted_at DESC, job_id DESC LIMIT ? OFFSET ?",
            [*params, query.page_size, (query.page - 1) * query.page_size],
        ).fetchall()
        items = [self._row_to_record(row) for row in rows]
        return SubmissionPage(
            generated_at=now,
            total=total,
            page=query.page,
            page_size=query.page_size,
            items=items,
        )

    def recent_failures(self, now: datetime, *, hours: int = 24, limit: int = 5) -> tuple[int, list[SubmissionRecord]]:
        cutoff = timestamp(now - timedelta(hours=hours))
        total = int(
            self.db.execute(
                "SELECT COUNT(*) FROM submission_audit "
                "WHERE status = 'failed' AND COALESCE(failure_updated_at, updated_at) >= ?",
                (cutoff,),
            ).fetchone()[0]
        )
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM submission_audit "
            "WHERE status = 'failed' AND COALESCE(failure_updated_at, updated_at) >= ? "
            "ORDER BY COALESCE(failure_updated_at, updated_at) DESC, job_id DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return total, [self._row_to_record(row) for row in rows]
