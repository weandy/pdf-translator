"""管理认证用例：固定会话期限、串行密码校验和有界单实例限流。"""

import asyncio
import hashlib
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import ceil

from pdftr.domain.admin import (
    SESSION_SECONDS,
    AdminIdentity,
    AdminPassword,
    AdminSession,
    AdminSessionStore,
    csrf_for,
    token_digest,
)


class InvalidAdminCredentials(Exception):
    pass


class AdminRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class WindowLimiter:
    """活动桶不被驱逐；容量满时拒绝新来源，避免制造新地址绕过失败次数。"""

    def __init__(self, limit: int, window_s: int, capacity: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self.capacity = capacity
        self.clock = clock
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for key in list(self._buckets):
            bucket = self._buckets[key]
            while bucket and bucket[0] <= now - self.window_s:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]

    def check(self, key: str, *, consume: bool = True) -> None:
        with self._lock:
            now = self.clock()
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.capacity:
                    expires = min(values[0] for values in self._buckets.values()) + self.window_s
                    raise AdminRateLimited(max(1, ceil(expires - now)))
                if consume:
                    self._buckets[key] = deque([now])
                return
            if len(bucket) >= self.limit:
                raise AdminRateLimited(max(1, ceil(bucket[0] + self.window_s - now)))
            if consume:
                bucket.append(now)


class AdminAuth:
    def __init__(self, store: AdminSessionStore, password_hash: str, origin: str) -> None:
        self.store = store
        self.origin = origin
        self.password = AdminPassword.parse(password_hash)
        self.password_version = hashlib.sha256(password_hash.encode("ascii")).hexdigest()
        self._password_lock = threading.Lock()
        self.login_failures = WindowLimiter(5, 15 * 60, 1024)
        self.login_attempts = WindowLimiter(30, 60, 1)
        self.queries = WindowLimiter(60, 60, 4096)
        self.purge()

    def purge(self) -> None:
        self.store.purge(datetime.now(UTC), self.password_version)

    async def login(self, password: str, client_ip: str | None) -> tuple[str, AdminIdentity]:
        source = client_ip or "unknown"
        self.login_attempts.check("global")
        self.login_failures.check(source, consume=False)
        await asyncio.to_thread(self._verify_password, password, source)
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        session = AdminSession(
            token_digest(token), now, now + timedelta(seconds=SESSION_SECONDS), self.password_version
        )
        self.store.save(session)
        return token, self._identity(session, token)

    def _verify_password(self, password: str, source: str) -> None:
        # 线程内持锁：即使 HTTP 协程取消，昂贵计算及失败计数仍串行完成。
        with self._password_lock:
            self.login_failures.check(source, consume=False)
            if not self.password.verify(password):
                self.login_failures.check(source)
                raise InvalidAdminCredentials

    @staticmethod
    def _identity(session: AdminSession, token: str) -> AdminIdentity:
        return AdminIdentity(
            session.token_hash, session.created_at, session.expires_at, session.password_version, csrf_for(token)
        )

    def authenticate(self, token: str | None) -> AdminIdentity:
        if token is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise InvalidAdminCredentials
        session = self.store.get(token_digest(token))
        if (
            session is None
            or session.password_version != self.password_version
            or session.expires_at <= datetime.now(UTC)
        ):
            raise InvalidAdminCredentials
        return self._identity(session, token)

    def logout(self, identity: AdminIdentity) -> None:
        self.store.delete(identity.token_hash)

    def enforce_query_rate(self, identity: AdminIdentity) -> None:
        self.queries.check(identity.token_hash)
