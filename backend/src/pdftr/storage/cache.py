"""译文缓存（SQLite）。按结构实现 translation.port.TranslationCache，不导入 translation 模块。"""

import sqlite3
import threading
from pathlib import Path


class SqliteTranslationCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS translation_cache ("
            " key TEXT PRIMARY KEY, target TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self._db.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT target FROM translation_cache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, target: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO translation_cache (key, target) VALUES (?, ?)", (key, target))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
