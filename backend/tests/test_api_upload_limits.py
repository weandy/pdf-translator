"""用真实 multipart 请求验证应用接收阶段的上传护栏。"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pdftr.api.app import create_app
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.pipeline import usecases
from pdftr.storage.cache import SqliteTranslationCache
from pdftr.storage.sqlite import SqliteStore

HEADERS = {"X-LLM-Key": "synthetic-key", "X-Parser-Key": "synthetic-token"}


@pytest.fixture
def setup_upload(tmp_path, monkeypatch):
    container = build_container(Settings(data_dir=tmp_path, glossary_sources="", max_upload_mb=1, min_free_disk_mb=1))
    pipeline = container.pipeline
    sizes = []
    receive = usecases.receive_upload

    async def tracked(pipeline, upload, *args, **kwargs):
        read = upload.read

        def tracked_read(size):
            data = read(size)
            sizes.append(len(data))
            return data

        monkeypatch.setattr(upload, "read", tracked_read)
        return await receive(pipeline, upload, *args, **kwargs)

    monkeypatch.setattr(usecases, "receive_upload", tracked)
    yield TestClient(create_app(container)), pipeline, sizes

    assert not pipeline.deps.store.all_jobs()
    assert not list(pipeline.deps.artifacts.root.iterdir())
    assert pipeline.receiving == 0
    assert isinstance(pipeline.deps.store, SqliteStore) and isinstance(pipeline.deps.cache, SqliteTranslationCache)
    pipeline.deps.cache.close()
    pipeline.deps.store.close()


def post_pdf(client, data):
    return client.post(
        "/api/jobs",
        files={"file": ("synthetic.pdf", data, "application/pdf")},
        data={"options": '{"cloud_consent":true}'},
        headers=HEADERS,
    )


@pytest.mark.parametrize(
    ("prefix", "padding", "message", "bytes_read"),
    [
        (b"%PDF", 2**20, "超过 1 MB", 2**20 + 1),
        (b"TEXT", 2**20, "只支持 PDF", 4),
        (b"%PDF-synthetic-invalid", 0, "无法打开", len(b"%PDF-synthetic-invalid")),
        (b"", 0, "只支持 PDF", 0),
    ],
)
def test_post_rejects_invalid_uploads(setup_upload, monkeypatch, prefix, padding, message, bytes_read):
    client, _, sizes = setup_upload
    if prefix != b"%PDF-synthetic-invalid":

        def unexpected(*args, **kwargs):
            pytest.fail("校验未通过时不应调用 PDF 解析")

        monkeypatch.setattr(usecases, "page_count", unexpected)
        if prefix != b"%PDF":
            monkeypatch.setattr(usecases.tempfile, "NamedTemporaryFile", unexpected)
    response = post_pdf(client, prefix + b"x" * padding)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert sum(sizes) == bytes_read


def test_post_checks_disk_space_before_creating_staging_file(setup_upload, monkeypatch):
    client, pipeline, sizes = setup_upload

    def full(path):
        assert path == pipeline.deps.artifacts.root
        return SimpleNamespace(free=0)

    def no_staging(*args, **kwargs):
        pytest.fail("磁盘不足时不应创建临时文件")

    monkeypatch.setattr(usecases.shutil, "disk_usage", full)
    monkeypatch.setattr(usecases.tempfile, "NamedTemporaryFile", no_staging)
    response = post_pdf(client, b"%PDF-synthetic")
    assert response.status_code == 503 and "存储空间不足" in response.json()["detail"]
    assert not sizes


def test_post_jobs_reserves_channel_slot_before_multipart_read(tmp_path, monkeypatch) -> None:
    import asyncio
    import threading

    import httpx2

    # 将 BYOK 通道总容量限制为 2（max_concurrent_jobs=1 + max_queued_jobs=1）
    c = build_container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            data_dir=tmp_path,
            glossary_sources="",
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            min_free_disk_mb=0,
        )
    )
    entered_two = threading.Event()
    release_body = threading.Event()
    in_flight = 0
    max_in_flight = 0
    guard = threading.Lock()
    orig_receive = usecases.receive_upload

    async def slow_receive(pipeline, upload, *args, **kwargs):
        nonlocal in_flight, max_in_flight
        with guard:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if in_flight >= 2:
                entered_two.set()
        try:
            await asyncio.to_thread(release_body.wait, 2.0)
            return await orig_receive(pipeline, upload, *args, **kwargs)
        finally:
            with guard:
                in_flight -= 1

    monkeypatch.setattr(usecases, "receive_upload", slow_receive)
    app = create_app(c)

    async def scenario() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:

            async def send_one() -> httpx2.Response:
                return await client.post(
                    "/api/jobs",
                    files={"file": ("bad.pdf", b"NOTPDF", "application/pdf")},
                    data={"options": '{"cloud_consent":true}'},
                    headers=HEADERS,
                )

            t1 = asyncio.create_task(send_one())
            t2 = asyncio.create_task(send_one())
            assert await asyncio.to_thread(entered_two.wait, 2.0)
            # t1、t2 占满 2 席仍在读取阶段时并发打入两个请求：必须在读取请求体前直接被中间件 503 拒绝
            r3, r4 = await asyncio.gather(send_one(), send_one())
            assert r3.status_code == 503
            assert r4.status_code == 503
            release_body.set()
            r1, r2 = await asyncio.gather(t1, t2)
            assert r1.status_code == 400
            assert r2.status_code == 400
            # 名额已准确归还：后续请求可正常进入（不被残留占位 503 锁死）
            r5 = await send_one()
            assert r5.status_code == 400

    try:
        asyncio.run(scenario())
        assert max_in_flight == 2
        assert c.pipeline.receiving == 0
    finally:
        c.close()
