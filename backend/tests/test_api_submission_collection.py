"""HTTP 接收上下文与管理响应的数据边界，全部使用合成输入。"""

import json
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.admin import hash_password
from pdftr.domain.submissions import NOTICE_VERSION, SubmissionRecord, SubmissionSearch
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore

ORIGIN = "https://admin.test"
PASSWORD = "synthetic-admin-password-2026"
HEADERS = {"X-LLM-Key": "synthetic-key", "X-Parser-Key": "synthetic-token"}


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.mark.parametrize("enabled", [False, True])
def test_upload_captures_only_when_enabled_and_admin_response_has_only_metadata(
    tmp_path: Path, monkeypatch, password_hash: str, enabled: bool
):
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            submission_audit_enabled=enabled,
            admin_password_hash=password_hash,
            admin_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
        )
    )
    monkeypatch.setattr(c.pipeline, "submit", lambda *args: None)
    client = TestClient(create_app(c), base_url=ORIGIN, client=("127.0.0.1", 50000))
    assert isinstance(c.pipeline.deps.store, SqliteStore)
    assert isinstance(c.pipeline.deps.cache, SqliteTranslationCache)
    try:
        with pymupdf.open() as document:
            document.new_page()
            data = document.tobytes()
        options = json.dumps(
            {"cloud_consent": True, "user_terms": {"synthetic-private-source": "synthetic-private-target"}}
        )
        response = client.post(
            "/api/jobs",
            files={"file": ("synthetic.pdf", data, "application/pdf")},
            data={"options": options},
            headers={**HEADERS, "X-Forwarded-For": "2001:db8::9"},
        )
        assert response.status_code == 202
        result = c.pipeline.deps.store.search_submissions(SubmissionSearch())
        assert result.total == int(enabled)
        if enabled:
            record = result.items[0]
            assert record.submitter_ip == "2001:db8::9" and record.ip_source == "trusted_proxy"
            assert record.notice_version == NOTICE_VERSION and record.page_count == 1
        login = client.post("/api/admin/session", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
        assert login.status_code == 200
        management_headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        listed = client.post("/api/admin/submissions/search", json={}, headers=management_headers)
        assert listed.status_code == 200 and listed.headers["Cache-Control"] == "no-store"
        assert listed.json()["total"] == int(enabled)
        if enabled:
            assert set(listed.json()["items"][0]) == set(SubmissionRecord.model_fields)
        for secret in ("synthetic-private-source", "synthetic-private-target", "synthetic-key", "synthetic-token"):
            assert secret not in listed.text
        invalid_ip = "synthetic-sensitive-search"
        error = client.post("/api/admin/submissions/search", json={"ip": invalid_ip}, headers=management_headers)
        assert error.status_code == 422 and invalid_ip not in error.text
        rejected = client.post(
            "/api/jobs",
            files={"file": ("synthetic.pdf", b"%PDF-broken", "application/pdf")},
            data={"options": options},
            headers=HEADERS,
        )
        assert rejected.status_code == 400
        assert c.pipeline.deps.store.search_submissions(SubmissionSearch()).total == int(enabled)
    finally:
        client.close()
        if c.admin:
            c.admin.store.close()
        c.pipeline.deps.store.close()
        c.pipeline.deps.cache.close()
