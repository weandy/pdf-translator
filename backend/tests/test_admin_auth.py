"""管理接口的 HTTPS Cookie、Origin、CSRF、限流及正文边界。"""

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdftr.api.admin_deps import AdminSecurityMiddleware
from pdftr.api.app import _purge_periodically, create_app
from pdftr.config import Settings
from pdftr.container import Container, build_container
from pdftr.domain.admin import SESSION_COOKIE, hash_password

PASSWORD = "synthetic-admin-password-2026"
ORIGIN = "https://admin.test"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture()
def system(tmp_path: Path, password_hash: str) -> Iterator[tuple[TestClient, Container]]:
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]  # BaseSettings 支持；阻止测试读取部署 .env。
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            cors_origins=["https://other.test"],
        )
    )
    client = TestClient(create_app(c), base_url=ORIGIN)
    try:
        yield client, c
    finally:
        client.close()
        if c.admin:
            c.admin.store.close()
        c.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def _login(client: TestClient, password: str = PASSWORD):
    return client.post("/api/admin/session", json={"password": password}, headers={"Origin": ORIGIN})


def test_secure_session_cookie_stable_csrf_and_revocation(system):
    client, _ = system
    response = _login(client)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    cookie = response.headers["Set-Cookie"]
    for required in (SESSION_COOKIE, "HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=2592000"):
        assert required in cookie
    assert "Domain=" not in cookie
    first = response.json()
    assert set(first) == {"csrf_token", "expires_at"}
    for _ in range(3):
        current = client.get("/api/admin/session")
        assert current.json() == first
        assert "Set-Cookie" not in current.headers
    headers = {"Origin": ORIGIN, "X-CSRF-Token": first["csrf_token"]}
    response = client.request("DELETE", "/api/admin/session", json={}, headers=headers)
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert client.get("/api/admin/session").status_code == 401


@pytest.mark.parametrize("origin", [None, "null", "https://other.test", ORIGIN + "/", ORIGIN + ".evil.test"])
def test_login_requires_exact_origin(system, origin):
    client, _ = system
    response = client.post(
        "/api/admin/session", json={"password": PASSWORD}, headers={"Origin": origin} if origin else {}
    )
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


def test_json_media_and_body_size_checked_before_authentication(system):
    client, _ = system
    assert client.post("/api/admin/session", data={"password": PASSWORD}, headers={"Origin": ORIGIN}).status_code == 415
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    assert client.post("/api/admin/session", content="{" + " " * 8192, headers=headers).status_code == 413
    assert client.post("/api/admin/session", content="invalid", headers=headers).status_code == 422
    assert client.post("/api/admin/session", content="[" * 2000 + "]" * 2000, headers=headers).status_code == 422
    too_long = "a" * 257
    response = client.post("/api/admin/session", json={"password": too_long}, headers=headers)
    assert response.status_code == 422
    assert too_long not in response.text
    assert "input" not in response.json()["detail"][0]
    assert client.post("/api/admin/session", json={"password": "中" * 86}, headers=headers).status_code == 422
    assert (
        client.post("/api/admin/session", json={"password": PASSWORD, "other": True}, headers=headers).status_code
        == 422
    )
    marker = "synthetic-secret-field-marker"
    response = client.post("/api/admin/session", json={"password": PASSWORD, marker: True}, headers=headers)
    assert response.status_code == 422
    assert marker not in response.text
    assert (
        client.post("/api/admin/session", content=iter([b" " * 4096, b" " * 4097]), headers=headers).status_code == 413
    )
    assert _login(client).status_code == 200


def test_csrf_bound_to_current_session(system):
    client, _ = system
    first = _login(client).json()["csrf_token"]
    second = _login(client).json()["csrf_token"]
    assert first != second
    for token in (None, "invalid", first):
        headers = {"Origin": ORIGIN}
        if token:
            headers["X-CSRF-Token"] = token
        assert client.request("DELETE", "/api/admin/session", json={}, headers=headers).status_code == 403
    assert client.get("/api/admin/session").json()["csrf_token"] == second
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, "unknown")
    assert client.get("/api/admin/session").status_code == 401


def test_unknown_ip_login_buckets_cannot_be_bypassed_by_spoofed_xff(system):
    client, _ = system
    for number in range(5):
        response = client.post(
            "/api/admin/session",
            json={"password": "wrong"},
            headers={"Origin": ORIGIN, "X-Forwarded-For": f"192.0.2.{number + 1}"},
        )
        assert response.status_code == 401
    response = _login(client)
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 900


def test_query_dependencies_stop_unauthorized_and_limit_valid_sessions(system):
    client, _ = system
    headers = {"Origin": ORIGIN}
    assert client.post("/api/admin/submissions/search", json={}, headers=headers).status_code == 401
    headers["X-CSRF-Token"] = _login(client).json()["csrf_token"]
    for _ in range(60):
        response = client.post("/api/admin/submissions/search", json={}, headers=headers)
        assert response.status_code == 200
    response = client.post("/api/admin/submissions/search", json={}, headers=headers)
    assert response.status_code == 429
    assert response.headers["Cache-Control"] == "no-store"


def test_missing_config_disables_all_management_endpoints(tmp_path: Path):
    c = build_container(Settings(_env_file=None, data_dir=tmp_path, glossary_sources=""))  # type: ignore[call-arg]
    client = TestClient(create_app(c), base_url=ORIGIN)
    try:
        assert c.admin is None
        for method, path in (
            ("GET", "/api/admin/session"),
            ("POST", "/api/admin/session"),
            ("DELETE", "/api/admin/session"),
            ("POST", "/api/admin/submissions/search"),
        ):
            response = client.request(method, path)
            assert response.status_code == 404
            assert response.headers["Cache-Control"] == "no-store"
    finally:
        client.close()
        c.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c.pipeline.deps.cache.close()  # type: ignore[attr-defined]


def test_search_storage_failure_returns_safe_uncached_error(system, monkeypatch):
    client, c = system
    marker = "synthetic-exception-secret-marker"

    def failing_search(query):
        raise RuntimeError(marker)

    monkeypatch.setattr(c.pipeline.deps.store, "search_submissions", failing_search)
    with TestClient(client.app, base_url=ORIGIN, raise_server_exceptions=False) as errors:
        csrf_token = _login(errors).json()["csrf_token"]
        response = errors.post(
            "/api/admin/submissions/search",
            json={},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 500
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {"detail": "管理请求处理失败，请稍后重试"}
        assert marker not in response.text
        assert "access-control-allow-origin" not in response.headers


def test_admin_boundary_preserves_cancellation_and_never_sends_second_headers(system):
    _, c = system
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError

    async def started_then_failed(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("synthetic-started-error")

    async def run():
        scope = {"type": "http", "path": "/api/admin/session", "method": "GET", "headers": []}
        with pytest.raises(asyncio.CancelledError):
            await AdminSecurityMiddleware(cancelled, c.admin)(scope, receive, send)
        assert not messages
        with pytest.raises(RuntimeError, match="synthetic-started-error"):
            await AdminSecurityMiddleware(started_then_failed, c.admin)(scope, receive, send)
        assert len(messages) == 1
        assert messages[0]["type"] == "http.response.start"

    asyncio.run(run())


def test_admin_session_cleanup_repeats_even_if_task_cleanup_fails(system, monkeypatch):
    _, c = system
    second_run = threading.Event()
    calls = 0
    assert c.admin is not None

    def fail_task_cleanup(*args):
        raise OSError("synthetic-cleanup-failure")

    def session_cleanup():
        nonlocal calls
        calls += 1
        if calls >= 2:
            second_run.set()

    monkeypatch.setattr("pdftr.api.app.purge_expired", fail_task_cleanup)
    monkeypatch.setattr(c.admin, "purge", session_cleanup)

    async def run():
        task = asyncio.create_task(_purge_periodically(c, interval_s=0.001))
        try:
            assert await asyncio.to_thread(second_run.wait, 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert calls >= 2
