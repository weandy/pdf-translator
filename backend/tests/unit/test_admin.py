"""认证用例与有界限流的合成验证，不使用部署环境或真实密码。"""

import asyncio
import hashlib
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdftr.config import Settings
from pdftr.domain.admin import AdminPassword, csrf_for, hash_password, token_digest
from pdftr.pipeline.admin import AdminAuth, AdminRateLimited, InvalidAdminCredentials, WindowLimiter
from pdftr.storage.admin import SqliteAdminSessionStore

PASSWORD = "synthetic-admin-password-2026"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


def test_hash_format_limits_and_constant_parameter_verification(password_hash: str):
    parsed = AdminPassword.parse(password_hash)
    assert parsed.verify(PASSWORD)
    assert not parsed.verify("synthetic-wrong-password")
    assert password_hash != hash_password(PASSWORD)
    for invalid in (password_hash.replace("$32768$", "$1048576$"), password_hash + "=", "bad"):
        with pytest.raises(ValueError):
            AdminPassword.parse(invalid)
    for invalid_password in ("short", "中" * 86):
        with pytest.raises(ValueError):
            hash_password(invalid_password)


@pytest.mark.parametrize(
    "origin",
    [
        "http://admin.test",
        "https://admin.test/",
        "https://user@admin.test",
        "https://admin.test?q=1",
        "https://admin.test#fragment",
        "null",
        "https://admin.test:invalid",
        "https://admin.test ",
    ],
)
def test_invalid_management_origin_is_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_origin=origin)  # type: ignore[call-arg]


def test_audit_settings_require_all_explicit_configuration(password_hash: str):
    assert not Settings(_env_file=None).submission_audit_enabled  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, submission_audit_enabled=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trusted_proxy_cidrs="0.0.0.0/0")  # type: ignore[call-arg]
    marker = "synthetic-invalid-hash-marker"
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, admin_password_hash=marker)  # type: ignore[call-arg]
    assert marker not in str(error.value)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        submission_audit_enabled=True,
        admin_password_hash=password_hash,
        admin_origin="https://admin.test:9443",
        trusted_proxy_cidrs="127.0.0.1/32, ::1/128",
    )
    assert settings.trusted_proxy_cidrs == ("127.0.0.1/32", "::1/128")
    assert password_hash not in repr(settings)
    parsed = Settings(_env_file=None, trusted_proxy_cidrs='["127.0.0.1/32"]')  # type: ignore[call-arg]
    assert parsed.trusted_proxy_cidrs == ("127.0.0.1/32",)


def test_persisted_digest_restart_rotation_expiry_and_logout(tmp_path: Path, password_hash: str):
    path = tmp_path / "app.db"
    first_store = SqliteAdminSessionStore(path)
    first_auth = AdminAuth(first_store, password_hash, "https://admin.test")
    token, identity = asyncio.run(first_auth.login(PASSWORD, "192.0.2.1"))
    assert len(token) == 43
    assert identity.token_hash == token_digest(token) == hashlib.sha256(token.encode()).hexdigest()
    assert identity.csrf_token == csrf_for(token)
    assert identity.expires_at - identity.created_at == timedelta(hours=8)
    first_store.close()
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT * FROM admin_sessions").fetchone()
        assert token not in str(row)
        assert PASSWORD not in str(row)
    store = SqliteAdminSessionStore(path)
    try:
        auth = AdminAuth(store, password_hash, "https://admin.test")
        assert auth.authenticate(token) == identity
        assert auth.authenticate(token).expires_at == identity.expires_at
        auth.logout(identity)
        with pytest.raises(InvalidAdminCredentials):
            auth.authenticate(token)
        token, identity = asyncio.run(auth.login(PASSWORD, None))
        store.delete(identity.token_hash)
        store.save(replace(identity, expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        with pytest.raises(InvalidAdminCredentials):
            auth.authenticate(token)
        auth.purge()
        assert store.get(identity.token_hash) is None
        token, _ = asyncio.run(auth.login(PASSWORD, None))
        rotated = AdminAuth(store, hash_password(PASSWORD + "-rotated"), "https://admin.test")
        with pytest.raises(InvalidAdminCredentials):
            rotated.authenticate(token)
    finally:
        store.close()


def test_concurrent_failures_only_run_five_expensive_verifications(tmp_path: Path, password_hash: str):
    store = SqliteAdminSessionStore(tmp_path / "app.db")
    auth = AdminAuth(store, password_hash, "https://admin.test")
    calls = 0

    class FakePassword:
        def verify(self, password: str) -> bool:
            nonlocal calls
            calls += 1
            return False

    auth.password = FakePassword()  # type: ignore[assignment]

    async def attempt() -> list:
        return await asyncio.gather(*(auth.login("wrong", None) for _ in range(12)), return_exceptions=True)

    try:
        results = asyncio.run(attempt())
        assert calls == 5
        assert sum(isinstance(result, InvalidAdminCredentials) for result in results) == 5
        assert sum(isinstance(result, AdminRateLimited) for result in results) == 7
    finally:
        store.close()


def test_limiter_has_expiry_global_limit_and_bounded_capacity():
    now = [0.0]
    limiter = WindowLimiter(2, 60, 2, lambda: now[0])
    limiter.check("a")
    limiter.check("a")
    with pytest.raises(AdminRateLimited) as error:
        limiter.check("a")
    assert error.value.retry_after == 60
    limiter.check("b")
    with pytest.raises(AdminRateLimited):
        limiter.check("c")
    assert len(limiter._buckets) == 2
    now[0] = 60
    limiter.check("c")
    assert list(limiter._buckets) == ["c"]
    global_limit = WindowLimiter(30, 60, 1)
    for _ in range(30):
        global_limit.check("global")
    with pytest.raises(AdminRateLimited):
        global_limit.check("global")


def test_cancelled_login_keeps_work_serialized_and_counts_failure(tmp_path: Path, password_hash: str):
    store = SqliteAdminSessionStore(tmp_path / "app.db")
    auth = AdminAuth(store, password_hash, "https://admin.test")
    started = threading.Event()
    release = threading.Event()
    active = 0
    maximum = 0

    class BlockingPassword:
        def verify(self, password: str) -> bool:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            started.set()
            assert release.wait(2)
            active -= 1
            return False

    auth.password = BlockingPassword()  # type: ignore[assignment]

    async def run() -> None:
        first = asyncio.create_task(auth.login("wrong", None))
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(auth.login("wrong", None))
        release.set()
        with pytest.raises(InvalidAdminCredentials):
            await second

    try:
        asyncio.run(run())
        assert maximum == 1
        assert len(auth.login_failures._buckets["unknown"]) == 2
    finally:
        release.set()
        store.close()
