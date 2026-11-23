"""译文缓存（SQLite）。按结构实现 translation.port.TranslationCache，不导入 translation 模块。"""

import threading
from pathlib import Path

from pdftr.storage.connection import connect, retry_busy, write_tx


class SqliteTranslationCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = connect(path)
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    @retry_busy
    def _initialize(self) -> None:
        self._db.execute("PRAGMA journal_mode=WAL")
        # 缓存含论文原文/译文，保留期删除也应覆盖主库中释放的单元格。
        self._db.execute("PRAGMA secure_delete=ON")
        with self._lock, write_tx(self._db):
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS translation_cache ("
                " key TEXT PRIMARY KEY, target TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    @retry_busy
    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT target FROM translation_cache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @retry_busy
    def put(self, key: str, target: str) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute("INSERT OR REPLACE INTO translation_cache (key, target) VALUES (?, ?)", (key, target))

    @retry_busy
    def purge_older_than(self, hours: int) -> int:
        """删除超过保留期的译文（其中含有用户文档内容），返回删除条数。"""
        with self._lock, write_tx(self._db):
            n = self._db.execute(
                "DELETE FROM translation_cache WHERE created_at < datetime('now', ?)", (f"-{hours} hours",)
            ).rowcount
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()
