"""只将允许长期保留的表写入全新库，不复制正文所在的库页、WAL或管理会话。"""

import os
import sqlite3
from pathlib import Path

TABLE_COLUMNS = {
    "analytics_meta": ("key", "value"),
    "analytics_daily": ("day", "page_views", "visitors", "jobs_submitted", "jobs_completed", "pages_submitted"),
    "analytics_jobs": ("job_id", "completed"),
    "analytics_visitors": ("visitor_hash",),
    "submission_audit": (
        "job_id",
        "title",
        "title_source",
        "submitted_at",
        "submitter_ip",
        "ip_source",
        "page_count",
        "status",
        "first_completed_at",
        "updated_at",
        "record_source",
        "notice_version",
        "current_stage",
        "failure_code",
        "failure_reason",
        "retryable",
        "failure_updated_at",
    ),
}
INTEGER_COLUMNS = {
    "page_views",
    "visitors",
    "jobs_submitted",
    "jobs_completed",
    "pages_submitted",
    "completed",
    "page_count",
    "retryable",
}


def export_metadata(source: Path, target: Path) -> dict[str, int]:
    if source.resolve() == target.resolve():
        raise ValueError("备份目标不能覆盖源数据库")
    if not source.is_file():
        raise ValueError("源数据库不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL防止覆盖既有文件；失败时保留不完整输出供检查，不删除调用者文件。
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    counts = {}
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        original.execute("BEGIN")
        try:
            with sqlite3.connect(target) as backup:
                for table, approved in TABLE_COLUMNS.items():
                    info = original.execute(f'PRAGMA table_info("{table}")').fetchall()
                    if not info:
                        continue
                    present = {row[1] for row in info}
                    columns = [column for column in approved if column in present]
                    if not columns:
                        continue
                    # 不复制源schema的默认值或新列，防止未来扩展把正文带入长期备份。
                    definitions = [
                        f'"{column}" {"INTEGER" if column in INTEGER_COLUMNS else "TEXT"}' for column in columns
                    ]
                    primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5] and row[1] in columns]
                    if primary:
                        definitions.append("PRIMARY KEY (" + ",".join(f'"{column}"' for column in primary) + ")")
                    backup.execute(f'CREATE TABLE "{table}" ({",".join(definitions)})')
                    names = ",".join(f'"{column}"' for column in columns)
                    cursor = original.execute(f'SELECT {names} FROM "{table}"')
                    placeholders = ",".join("?" for _ in cursor.description)
                    count = 0
                    while rows := cursor.fetchmany(1000):
                        backup.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', rows)
                        count += len(rows)
                    counts[table] = count
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("元数据备份完整性检查失败")
        finally:
            original.rollback()
    return counts
