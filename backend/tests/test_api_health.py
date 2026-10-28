from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.storage.files import FileArtifactStore


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(build_container(Settings(data_dir=tmp_path))))


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_dir_writable"] is True
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_artifact_store_rejects_traversal(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    ok = store.path("0123456789abcdef", "parser/raw.zip")
    assert ok.parent.name == "parser"
    with pytest.raises(ValueError):
        store.path("0123456789abcdef", "../escape.txt")
    with pytest.raises(ValueError):
        store.job_dir("../../etc")
