import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import cast

import pytest

from pdftr.api.routes_exports import _BundleResponse
from pdftr.domain.zip_names import zip_filenames
from pdftr.pipeline import bundle
from pdftr.pipeline.runner import ExportResult, Pipeline
from pdftr.pipeline.usecases import NotFound, ServiceBusy


def test_zip_names_are_flat_windows_safe_and_utf8_bounded():
    names = zip_filenames(
        [
            "../path\\正文\x00.pdf",
            "CON.pdf",
            "COM¹.pdf",
            "NUL .extra.pdf",
            "标题.pdf . ",
            "ＴＥＳＴ.pdf",
            "test.pdf",
            "文" * 150 + ".pdf",
            "文" * 150 + ".pdf",
        ]
    )
    assert names[:7] == [
        "_path_正文.pdf",
        "_CON.pdf",
        "_COM1.pdf",
        "_NUL .extra.pdf",
        "标题.pdf",
        "TEST.pdf",
        "test (2).pdf",
    ]
    assert names[7] != names[8]
    assert all(len(name.encode()) <= 200 for name in names)


def test_zip_names_strip_only_one_pdf_suffix():
    assert zip_filenames(["foo.PDF.pdf", "bar.PdF"]) == ["foo.PDF.pdf", "bar.pdf"]


def test_ticket_expiry_capacity_and_atomic_claim(monkeypatch):
    now = 100.0
    monkeypatch.setattr(bundle, "time", SimpleNamespace(monotonic=lambda: now))
    registry = bundle.BundleRegistry()
    try:
        issued = [registry.issue((), 22) for _ in range(128)]
        with pytest.raises(ServiceBusy):
            registry.issue((), 22)
        barrier = Barrier(2)

        def claim():
            barrier.wait(2)
            try:
                return registry.claim(issued[0])
            except NotFound:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            assert sum(f.result(2) is not None for f in futures) == 1
        now += 180
        with pytest.raises(NotFound):
            registry.claim(issued[1])
        assert registry.issue((), 22)
        with (
            registry.stream_slot(),
            registry.stream_slot(),
            registry.stream_slot(),
            pytest.raises(ServiceBusy),
            registry.stream_slot(),
        ):
            pytest.fail("打开了第 4 个 ZIP 下载")
    finally:
        registry.close()


def test_disconnect_releases_file_job_access_and_slot(tmp_path, monkeypatch):
    path = tmp_path / "export.pdf"
    path.write_bytes(b"x" * 80000)
    item = bundle.BundleItem("0" * 16, "mono", "right", "1" * 16)
    records = []
    accesses = set()

    @contextmanager
    def access(job_id):
        accesses.add(job_id)
        try:
            yield True
        finally:
            accesses.remove(job_id)

    monkeypatch.setattr(bundle, "get_job", lambda *_: None)
    monkeypatch.setattr(bundle, "published_export", lambda *_: ExportResult("a.pdf", path, 80000, crc32=0))
    real_open = Path.open

    def tracked(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == path:
            records.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked)
    pipeline = cast(Pipeline, SimpleNamespace(export_access=access))
    registry = bundle.BundleRegistry()
    try:
        token, size = bundle.issue_bundle(pipeline, registry, [item])
        assert not accesses and size > 80000

        async def interrupted():
            header_sent = asyncio.Event()

            async def receive():
                await header_sent.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    assert message["status"] == 200 and item.job_id in accesses
                if message["type"] == "http.response.body":
                    assert message["body"].startswith(b"PK\x03\x04")
                    header_sent.set()
                    await asyncio.Future()

            response = _BundleResponse(registry, pipeline, token)
            await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), 3)

        asyncio.run(interrupted())
        assert records and all(file.closed for file in records)
        assert not accesses
        with registry.stream_slot(), registry.stream_slot(), registry.stream_slot():
            pass
    finally:
        registry.close()
