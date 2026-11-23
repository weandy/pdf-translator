"""同一个数据库的任务仓储、译文缓存和管理员会话共用连接参数和锁冲突处理。"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from time import monotonic, sleep

BUSY_TIMEOUT_S = 15
_RETRY_DELAYS = (0.025, 0.05)


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
    try:
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def write_tx(db: sqlite3.Connection) -> Iterator[None]:
    """显式结束失败的事务，重试时从干净的连接状态开始。"""
    try:
        db.execute("BEGIN IMMEDIATE")
        yield
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def retry_busy[**P, T](operation: Callable[P, T]) -> Callable[P, T]:
    @wraps(operation)
    def run(*args: P.args, **kwargs: P.kwargs) -> T:
        started = monotonic()
        for delay in (*_RETRY_DELAYS, None):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as e:
                code = getattr(e, "sqlite_errorcode", None)
                busy = (
                    code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    if code is not None
                    else any(word in str(e).lower() for word in ("locked", "busy"))
                )
                if not busy or delay is None or monotonic() - started >= BUSY_TIMEOUT_S:
                    raise
                # 方法的事务和连接锁均已退出，可从头重试。
                sleep(delay)
        raise AssertionError("无法到达")

    return run
