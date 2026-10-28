"""任务与段落的 SQLite 仓储（与译文缓存共用 data/app.db）。

段落整体存为 JSON，另存状态列便于查询；数据量为每篇论文几百行，足够简单可靠。
"""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pdftr.domain.ir import Segment, SegmentStatus
from pdftr.domain.job import Job, JobStatus

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
"""


class SqliteStore:
    """同一个连接上的任务仓储与段落仓储。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ---- 任务 ----
    def save_job(self, job: Job) -> None:
        job.updated_at = datetime.now(UTC)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO jobs (id, status, data, created_at) VALUES (?, ?, ?, ?)",
                (job.id, job.status, job.model_dump_json(), job.created_at.isoformat()),
            )
            self._db.commit()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM segments WHERE job_id = ?", (job_id,))
            self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self._db.commit()

    def jobs_created_before(self, cutoff: datetime) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT id FROM jobs WHERE created_at < ?", (cutoff.isoformat(),)).fetchall()
        return [r[0] for r in rows]

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

    def close(self) -> None:
        with self._lock:
            self._db.close()
