"""任务与段落的 SQLite 仓储（与译文缓存共用 data/app.db）。

段落整体存为 JSON，另存状态列便于查询；数据量为每篇论文几百行，足够简单可靠。
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pdftr.domain.ir import Revision, Segment, SegmentStatus
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.stats import StatsOut
from pdftr.domain.submissions import SubmissionContext, SubmissionPage, SubmissionSearch
from pdftr.storage.stats import SCHEMA as STATS_SCHEMA
from pdftr.storage.stats import SqliteStats, utc_now
from pdftr.storage.submissions import SCHEMA as SUBMISSIONS_SCHEMA
from pdftr.storage.submissions import SqliteSubmissions
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
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # 覆盖主库逻辑删除的单元格；WAL 和既有备份仍需独立的保留策略。
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.executescript(_SCHEMA + STATS_SCHEMA + SUBMISSIONS_SCHEMA)
        ensure_submission_columns(self._db)
        self._stats = SqliteStats(self._db)
        self._submissions = SqliteSubmissions(self._db)
        self._submission_audit_enabled = submission_audit_enabled
        with self._db:
            self._stats.initialize(utc_now())

    # ---- 任务 ----
    def create_job(self, job: Job, context: SubmissionContext | None = None) -> None:
        """首次成功接收的任务、公开统计、可选账本必须原子提交。"""
        now = utc_now()
        job.updated_at = now
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO jobs (id, status, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
            self._stats.job_saved(job, now)
            if self._submission_audit_enabled:
                self._submissions.create(job, context, now)

    def save_job(self, job: Job) -> None:
        now = utc_now()
        job.updated_at = now
        with self._lock, self._db:
            completed = self._db.execute("SELECT completed FROM analytics_jobs WHERE job_id = ?", (job.id,)).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (id, status, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
            self._stats.job_saved(job, now)
            if self._submission_audit_enabled:
                # 统计去重状态在停采时也保留，避免恢复采集后伪造真正的首次完成时间。
                first_completion = job.status is JobStatus.DONE and not (completed and completed[0])
                self._submissions.job_saved(job, now, first_completion=first_completion)

    def record_visit(self, event_id: str, visitor_id: str) -> None:
        with self._lock, self._db:
            self._stats.visit(event_id, visitor_id, utc_now())

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        # 连接锁只能隔离本进程，显式读事务还防止其他连接在多个 SELECT 之间提交。
        self._db.execute("BEGIN")
        try:
            yield
        finally:
            self._db.rollback()

    def stats(self, days: int = 7) -> StatsOut:
        with self._lock, self._read_snapshot():
            return self._stats.summary(days, utc_now())

    def search_submissions(self, query: SubmissionSearch) -> SubmissionPage:
        with self._lock, self._read_snapshot():
            return self._submissions.search(query, utc_now())

    def update_submission_title(self, job_id: str, title: str) -> None:
        if self._submission_audit_enabled:
            with self._lock, self._db:
                self._submissions.update_title(job_id, title, utc_now())

    def backfill_submissions(self, titles: dict[str, str] | None = None) -> int:
        """显式历史迁移入口；不自动运行，不修改原统计或猜测 IP、首次完成时间。"""
        if not self._submission_audit_enabled:
            return 0
        with self._lock, self._db:
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

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def delete_job(self, job_id: str) -> None:
        with self._lock, self._db:
            self._queue_artifact_cleanup(job_id)
            self._db.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._db.execute("DELETE FROM revisions WHERE job_id = ?", (job_id,))
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def _queue_artifact_cleanup(self, job_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO artifact_cleanup (job_id, queued_at) VALUES (?, ?)",
            (job_id, utc_now().isoformat()),
        )

    def queue_artifact_cleanup(self, job_id: str) -> None:
        with self._lock, self._db:
            self._queue_artifact_cleanup(job_id)

    def pending_artifact_cleanup(self) -> list[str]:
        with self._lock:
            return [
                row[0] for row in self._db.execute("SELECT job_id FROM artifact_cleanup ORDER BY queued_at, job_id")
            ]

    def complete_artifact_cleanup(self, job_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM artifact_cleanup WHERE job_id = ?", (job_id,))

    def checkpoint_after_purge(self) -> None:
        # PASSIVE 不强等活动读者，不做耗时、需要额外磁盘空间的在线 VACUUM。
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def all_jobs(self) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs").fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def jobs_with_status(self, status: JobStatus) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs WHERE status = ?", (status,)).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    # ---- 段落 ----
    def replace_segments(self, job_id: str, segments: list[Segment]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._db.executemany(
                "INSERT INTO segments (id, job_id, idx, status, data) VALUES (?, ?, ?, ?, ?)",
                [(s.id, job_id, i, s.status, s.model_dump_json()) for i, s in enumerate(segments)],
            )
            self._db.commit()

    def save_segments(self, segments: list[Segment]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE segments SET status = ?, data = ? WHERE id = ?",
                [(s.status, s.model_dump_json(), s.id) for s in segments],
            )
            self._db.commit()

    def segments(self, job_id: str) -> list[Segment]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM segments WHERE job_id = ? ORDER BY idx", (job_id,)).fetchall()
        return [Segment.model_validate_json(r[0]) for r in rows]

    def segment(self, segment_id: str) -> Segment | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM segments WHERE id = ?", (segment_id,)).fetchone()
        return Segment.model_validate_json(row[0]) if row else None

    def count_segments(self, job_id: str, status: SegmentStatus) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM segments WHERE job_id = ? AND status = ?", (job_id, status)
            ).fetchone()
        return int(row[0])

    # ---- 修改记录 ----
    def add_revision(self, rev: Revision) -> Revision:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO revisions (job_id, segment_id, data) VALUES (?, ?, '')", (rev.job_id, rev.segment_id)
            )
            rev = rev.model_copy(update={"id": cur.lastrowid})
            self._db.execute("UPDATE revisions SET data = ? WHERE id = ?", (rev.model_dump_json(), rev.id))
            self._db.commit()
        return rev

    def save_revisions(self, revs: list[Revision]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE revisions SET data = ? WHERE id = ?", [(r.model_dump_json(), r.id) for r in revs]
            )
            self._db.commit()

    def revisions(self, job_id: str) -> list[Revision]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM revisions WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        return [Revision.model_validate_json(r[0]) for r in rows]

    def revision(self, revision_id: int) -> Revision | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        return Revision.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
