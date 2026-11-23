"""管理员会话独立连接现有 app.db，仅持久化随机 Token 的摘要。"""

import threading
from datetime import datetime
from pathlib import Path

from pdftr.domain.admin import AdminSession
from pdftr.storage.connection import connect, retry_busy, write_tx


class SqliteAdminSessionStore:
    def __init__(self, path: Path) -> None:
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
        with self._lock, write_tx(self._db):
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS admin_sessions ("
                "token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
                "password_version TEXT NOT NULL)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS admin_sessions_expires ON admin_sessions(expires_at)")

    @retry_busy
    def save(self, session: AdminSession) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute(
                "INSERT INTO admin_sessions (token_hash, created_at, expires_at, password_version) VALUES (?, ?, ?, ?)",
                (
                    session.token_hash,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                    session.password_version,
                ),
            )

    @retry_busy
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

    @retry_busy
    def delete(self, token_hash: str) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    @retry_busy
    def purge(self, now: datetime, password_version: str) -> None:
        with self._lock, write_tx(self._db):
            self._db.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ? OR password_version != ?",
                (now.isoformat(), password_version),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
