"""存储短期版面缓存与任务引用，使用独立 SQLite 文件。"""

import logging
import shutil
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from pdftr.domain.layout import LayoutResult
from pdftr.storage.connection import connect, retry_busy, write_tx

MAX_ENTRIES = 200
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
log = logging.getLogger(__name__)


def _timestamp(now_ts: int | None) -> int:
    return int(time.time()) if now_ts is None else now_ts


class SqliteLayoutCache:
    def __init__(
        self,
        path: Path,
        *,
        min_free_disk_mb: int = 0,
        max_entries: int = MAX_ENTRIES,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_entry_bytes: int = MAX_ENTRY_BYTES,
        max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._min_free_bytes = min_free_disk_mb * 1024 * 1024
        self._max_entries = min(max_entries, MAX_ENTRIES)
        self._max_total_bytes = min(max_total_bytes, MAX_TOTAL_BYTES)
        self._max_entry_bytes = min(max_entry_bytes, MAX_ENTRY_BYTES)
        self._max_uncompressed_bytes = min(max_uncompressed_bytes, MAX_UNCOMPRESSED_BYTES)
        self._db = connect(path)
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    @retry_busy
    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute("PRAGMA foreign_keys=ON")
        with write_tx(self._db):
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS layout_cache (
                    cache_key TEXT PRIMARY KEY, layout_blob BLOB NOT NULL, compressed_bytes INTEGER NOT NULL,
                    raw_bytes INTEGER NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
                )"""
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS layout_cache_expires ON layout_cache(expires_at)")
            self._db.execute("CREATE INDEX IF NOT EXISTS layout_cache_lru ON layout_cache(created_at)")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS layout_cache_refs (
                    job_id TEXT NOT NULL, cache_key TEXT NOT NULL REFERENCES layout_cache(cache_key) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL, PRIMARY KEY (job_id, cache_key)
                )"""
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS layout_cache_refs_key ON layout_cache_refs(cache_key)")

    def get(self, key: str, *, job_id: str, now_ts: int | None = None) -> LayoutResult | None:
        row = self._get_and_reference(key, job_id, _timestamp(now_ts))
        if row is None:
            return None
        blob, raw_bytes, created_at = row
        try:
            if not isinstance(blob, bytes) or len(blob) > self._max_entry_bytes:
                raise ValueError("压缩数据超限")
            inflater = zlib.decompressobj()
            raw = inflater.decompress(blob, self._max_uncompressed_bytes + 1)
            if (
                len(raw) > self._max_uncompressed_bytes
                or len(raw) != raw_bytes
                or not inflater.eof
                or inflater.unused_data
            ):
                raise ValueError("解压数据超限或已损坏")
            return LayoutResult.model_validate_json(raw)
        except (ValueError, zlib.error):
            self._delete_corrupt(key, blob, created_at)
            log.warning("已移除损坏的版面缓存")
            return None

    @retry_busy
    def _get_and_reference(self, key: str, job_id: str, now: int) -> tuple[bytes, int, int] | None:
        with self._lock:
            with write_tx(self._db):
                row = self._db.execute(
                    """SELECT CASE WHEN length(layout_blob) <= ? THEN layout_blob END,
                              raw_bytes, created_at, expires_at
                       FROM layout_cache WHERE cache_key = ?""",
                    (self._max_entry_bytes, key),
                ).fetchone()
                if row is None:
                    return None
                if row[3] > now and row[0] is not None:
                    self._reference(job_id, key, now)
                    return row[0], row[1], row[2]
                self._db.execute("DELETE FROM layout_cache WHERE cache_key = ?", (key,))
            self._checkpoint()
        return None

    @retry_busy
    def _delete_corrupt(self, key: str, blob: bytes, created_at: int) -> None:
        with self._lock:
            with write_tx(self._db):
                # 解码在事务外进行，清除时校验快照以保留期间可能写入的新条目。
                removed = self._db.execute(
                    "DELETE FROM layout_cache WHERE cache_key = ? AND layout_blob = ? AND created_at = ?",
                    (key, blob, created_at),
                ).rowcount
            if removed:
                self._checkpoint()

    def put(self, key: str, layout: LayoutResult, *, job_id: str, ttl_seconds: int, now_ts: int | None = None) -> bool:
        if ttl_seconds <= 0 or self._max_entries < 1:
            return False
        if shutil.disk_usage(self._path.parent).free < self._min_free_bytes:
            log.warning("磁盘剩余空间不足，跳过写入版面缓存")
            return False
        raw = layout.model_dump_json().encode("utf-8")
        if len(raw) > self._max_uncompressed_bytes:
            return False
        blob = zlib.compress(raw, level=6)
        if len(blob) > min(self._max_entry_bytes, self._max_total_bytes):
            return False
        self._put(key, blob, len(raw), job_id, ttl_seconds, _timestamp(now_ts))
        return True

    @retry_busy
    def _put(self, key: str, blob: bytes, raw_bytes: int, job_id: str, ttl: int, now: int) -> None:
        with self._lock:
            with write_tx(self._db):
                removed = self._delete_expired(now)
                self._db.execute(
                    """INSERT OR IGNORE INTO layout_cache
                       (cache_key, layout_blob, compressed_bytes, raw_bytes, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (key, blob, len(blob), raw_bytes, now, now + ttl),
                )
                self._reference(job_id, key, now)
                rows = self._db.execute(
                    "SELECT cache_key, compressed_bytes FROM layout_cache ORDER BY created_at, rowid"
                ).fetchall()
                count = len(rows)
                total = sum(row[1] for row in rows)
                for old_key, size in rows:
                    if count <= self._max_entries and total <= self._max_total_bytes:
                        break
                    if old_key != key:
                        self._db.execute("DELETE FROM layout_cache WHERE cache_key = ?", (old_key,))
                        count -= 1
                        total -= size
                        removed += 1
            if removed:
                self._checkpoint()

    def _reference(self, job_id: str, key: str, now: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO layout_cache_refs (job_id, cache_key, created_at) VALUES (?, ?, ?)",
            (job_id, key, now),
        )

    def _delete_expired(self, now: int) -> int:
        return self._db.execute("DELETE FROM layout_cache WHERE expires_at <= ?", (now,)).rowcount

    @retry_busy
    def release_job(self, job_id: str) -> int:
        with self._lock:
            with write_tx(self._db):
                keys = self._db.execute(
                    "SELECT cache_key FROM layout_cache_refs WHERE job_id = ?", (job_id,)
                ).fetchall()
                refs = self._db.execute("DELETE FROM layout_cache_refs WHERE job_id = ?", (job_id,)).rowcount
                removed = self._db.executemany(
                    """DELETE FROM layout_cache WHERE cache_key = ?
                       AND NOT EXISTS (SELECT 1 FROM layout_cache_refs WHERE cache_key = layout_cache.cache_key)""",
                    keys,
                ).rowcount
            if refs:
                self._checkpoint()
            return removed

    @retry_busy
    def purge_expired(self, *, known_job_ids: set[str] | None = None, now_ts: int | None = None) -> int:
        now = _timestamp(now_ts)
        with self._lock:
            with write_tx(self._db):
                removed = self._delete_expired(now)
                orphan_refs = 0
                if known_job_ids is not None:
                    # 本轮任务快照之后新增的引用留给下轮扫描。
                    candidates = self._db.execute(
                        "SELECT DISTINCT job_id FROM layout_cache_refs WHERE created_at < ?", (now,)
                    ).fetchall()
                    orphan_refs = self._db.executemany(
                        "DELETE FROM layout_cache_refs WHERE job_id = ? AND created_at < ?",
                        [(job_id, now) for (job_id,) in candidates if job_id not in known_job_ids],
                    ).rowcount
                removed += self._db.execute(
                    """DELETE FROM layout_cache WHERE NOT EXISTS
                       (SELECT 1 FROM layout_cache_refs WHERE cache_key = layout_cache.cache_key)"""
                ).rowcount
            if removed or orphan_refs:
                self._checkpoint()
            return removed

    @retry_busy
    def clear_all(self) -> int:
        with self._lock:
            with write_tx(self._db):
                removed = self._db.execute("DELETE FROM layout_cache").rowcount
            self._checkpoint()
            return removed

    def _checkpoint(self) -> None:
        try:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            log.warning("版面缓存检查点执行失败")

    def close(self) -> None:
        with self._lock:
            self._db.close()
