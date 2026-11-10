"""管理员会话独立连接现有 app.db，仅持久化随机 Token 的摘要。"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from pdftr.domain.admin import AdminSession


class SqliteAdminSessionStore:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS admin_sessions ("
            "token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
            "password_version TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS admin_sessions_expires ON admin_sessions(expires_at);"
        )
        self._db.commit()

    def save(self, session: AdminSession) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO admin_sessions (token_hash, created_at, expires_at, password_version) VALUES (?, ?, ?, ?)",
                (
                    session.token_hash,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                    session.password_version,
                ),
            )

    def get(self, token_hash: str) -> AdminSession | None:
        with self._lock:
            row = self._db.execute(
                "SELECT token_hash, created_at, expires_at, password_version FROM admin_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return (
            AdminSession(row[0], datetime.fromisoformat(row[1]), datetime.fromisoformat(row[2]), row[3])
            if row
            else None
        )

    def delete(self, token_hash: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def purge(self, now: datetime, password_version: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR password_version != ?",
                (now.isoformat(), password_version),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
