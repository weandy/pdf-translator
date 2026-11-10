"""管理员查询只暴露批准字段，且权限校验不能依赖前端入口。"""

from pathlib import Path

from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.admin import hash_password
from pdftr.domain.job import Job
from pdftr.domain.submissions import SubmissionContext


def test_private_search_requires_session_csrf_and_keeps_public_stats_clean(tmp_path: Path):
    origin = "https://admin.test"
    password = "synthetic-admin-search-password"
    c = build_container(
        Settings(
            data_dir=tmp_path,
            glossary_sources="",
            admin_password_hash=hash_password(password),
            admin_origin=origin,
            trusted_proxy_cidrs=("127.0.0.1/32",),
            submission_audit_enabled=True,
        )
    )
    try:
        store = c.pipeline.deps.store
        store.create_job(
            Job(id="1234567890abcdef", filename="Synthetic_Audit_Paper.pdf", page_count=8),
            SubmissionContext(submitter_ip="192.0.2.8", ip_source="direct"),
        )
        with TestClient(create_app(c), base_url=origin) as client:
            headers = {"Origin": origin}
            assert client.get("/api/admin/submissions/client").status_code == 401
            assert client.post("/api/admin/submissions/search", json={}, headers=headers).status_code == 401
            login = client.post("/api/admin/session", json={"password": password}, headers=headers)
            assert login.status_code == 200
            network = client.get("/api/admin/submissions/client")
            assert network.status_code == 200
            assert network.headers["Cache-Control"] == "no-store"
            assert set(network.json()) == {"submitter_ip", "ip_source", "notice_version"}
            assert client.post("/api/admin/submissions/search", json={}, headers=headers).status_code == 403
            headers["X-CSRF-Token"] = login.json()["csrf_token"]
            response = client.post(
                "/api/admin/submissions/search", json={"title_query": "Audit", "ip": "192.0.2.8"}, headers=headers
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            body = response.json()
            assert body["total"] == 1
            assert body["items"][0]["submitter_ip"] == "192.0.2.8"
            assert "source" not in body["items"][0] and "target" not in body["items"][0]
            public = client.get("/api/stats").text
            assert "Synthetic" not in public and "192.0.2.8" not in public
            assert client.request("DELETE", "/api/admin/session", json={}, headers=headers).status_code == 204
            assert client.post("/api/admin/submissions/search", json={}, headers=headers).status_code == 401
    finally:
        if c.admin:
            c.admin.store.close()
        c.pipeline.deps.store.close()  # type: ignore[attr-defined]
        c.pipeline.deps.cache.close()  # type: ignore[attr-defined]
