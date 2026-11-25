"""补翻状态与发布事务的 SQL 操作，由 SqliteStore 管理连接和锁。"""

from collections.abc import Sequence
from sqlite3 import Connection

from pdftr.domain.expansion import ACTIVE_EXPANSION_STATUSES, ExpansionOp, ExpansionStage, ExpansionStatus
from pdftr.domain.ir import Segment
from pdftr.domain.job import Job, JobStatus
from pdftr.storage.stats import utc_now

EXPANSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_expansions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_expansions_job ON job_expansions (job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS job_expansions_active_one
    ON job_expansions (job_id) WHERE status IN ('queued', 'running');
"""


def load_expansion(db: Connection, job_id: str, op_id: str) -> ExpansionOp | None:
    row = db.execute("SELECT data FROM job_expansions WHERE job_id = ? AND id = ?", (job_id, op_id)).fetchone()
    return ExpansionOp.model_validate_json(row[0]) if row else None


def active_expansions(db: Connection, job_id: str | None = None) -> list[ExpansionOp]:
    sql = "SELECT data FROM job_expansions WHERE status IN ('queued', 'running')"
    rows = db.execute(sql if job_id is None else sql + " AND job_id = ?", () if job_id is None else (job_id,))
    return [ExpansionOp.model_validate_json(row[0]) for row in rows]


def save_expansion(db: Connection, op: ExpansionOp) -> None:
    db.execute(
        "UPDATE job_expansions SET status = ?, data = ? WHERE id = ?",
        (op.status, op.model_dump_json(), op.id),
    )


def _job(db: Connection, job_id: str) -> Job:
    row = db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("任务不存在")
    return Job.model_validate_json(row[0])


def _apply_cost(op: ExpansionOp, job: Job) -> bool:
    if op.cost_applied or op.cost_cny is None:
        return False
    job.cost_cny = (job.cost_cny or 0.0) + op.cost_cny
    op.cost_applied = True
    return True


def finish_expansion(db: Connection, job_id: str, op_id: str, status: ExpansionStatus, error: str) -> Job:
    op = load_expansion(db, job_id, op_id)
    job = _job(db, job_id)
    if op is None or op.status not in ACTIVE_EXPANSION_STATUSES:
        return job
    op.status, op.error, op.updated_at = status, error, utc_now()
    op.message = "已取消补翻" if status is ExpansionStatus.CANCELLED else "补翻失败"
    if _apply_cost(op, job):
        db.execute("UPDATE jobs SET data = ? WHERE id = ?", (job.model_dump_json(), job_id))
    save_expansion(db, op)
    return job


def publish_expansion(
    db: Connection,
    job_id: str,
    op_id: str,
    old_ids: set[str],
    new_segments: Sequence[Segment],
    ordered_ids: Sequence[str],
) -> Job:
    op = load_expansion(db, job_id, op_id)
    job = _job(db, job_id)
    if op is None or op.status is not ExpansionStatus.RUNNING:
        raise ValueError("补翻操作已结束")
    if job.status is not JobStatus.DONE or job.content_version != op.base_version:
        raise ValueError("已发布的译文版本已变化")
    ids = {row[0] for row in db.execute("SELECT id FROM segments WHERE job_id = ?", (job.id,))}
    if ids != old_ids:
        raise ValueError("已发布的段落已变化")
    positions = {segment_id: idx for idx, segment_id in enumerate(ordered_ids)}
    if len(positions) != len(ordered_ids) or positions.keys() != ids | {seg.id for seg in new_segments}:
        raise ValueError("段落顺序与发布内容不一致")
    db.executemany(
        "INSERT INTO segments (id, job_id, idx, status, data) VALUES (?, ?, ?, ?, ?)",
        [(seg.id, job.id, positions[seg.id], seg.status, seg.model_dump_json()) for seg in new_segments],
    )
    db.executemany(
        "UPDATE segments SET idx = ? WHERE id = ? AND job_id = ?",
        [(positions[segment_id], segment_id, job.id) for segment_id in old_ids],
    )
    job.content_version = op.target_version
    job.published_pages = op.merged_pages
    job.options.pages = None if len(op.merged_pages) == job.page_count else op.merged_pages
    job.message = f"已补翻 {len(op.requested_pages)} 页"
    job.fallback_used |= op.fallback_used
    job.updated_at = utc_now()
    _apply_cost(op, job)
    op.cost_applied = True
    op.status, op.stage, op.progress = ExpansionStatus.DONE, ExpansionStage.DONE, 1.0
    op.updated_at = job.updated_at
    op.message = job.message
    db.execute("UPDATE jobs SET data = ? WHERE id = ?", (job.model_dump_json(), job.id))
    save_expansion(db, op)
    return job
