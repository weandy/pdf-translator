import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pdftr.api.schemas import JobOut
from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.job import Job, JobStatus
from pdftr.domain.layout import LayoutResult
from pdftr.parsing.port import ParseError
from pdftr.pipeline import runner
from pdftr.pipeline.secrets import Secrets


def test_parse_progress_throttles_sqlite_and_sse(tmp_path, monkeypatch):
    now = 100.0
    rows = []
    events = []

    class Parser:
        async def parse(self, pdf, pages, raw_dir, on_progress):
            nonlocal now
            assert raw_dir is None
            for delta, message, fraction in [
                (0.0, "正在上传 1", 0.1),
                (0.2, "正在上传 2", 0.2),
                (0.2, "正在上传 3", 0.3),
                (0.1, "MinerU 已接收", None),
                (0.1, "MinerU 正在解析 1", 0.4),
                (0.3, "MinerU 正在解析 2", 0.5),
                (1.5, "MinerU 正在解析 3", 0.6),
                (0.1, "MinerU 正在解析完成", 1.0),
                (0.1, "MinerU 正在解析超时重试", None),
                (0.1, "正在下载", 1.0),
            ]:
                now += delta
                on_progress(message, fraction)
            return LayoutResult(parser="mineru", pages=[])

    pipeline = build_container(
        Settings(data_dir=tmp_path, layout_cache_ttl_hours=0),
        {"parser_factory": lambda *_: Parser()},
    ).pipeline
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(pipeline.deps.store, "save_job", lambda job: rows.append((job.message, job.progress)))
    monkeypatch.setattr(pipeline.deps.events, "publish", lambda job: events.append((job.message, job.progress)))
    secrets = Secrets(llm_base_url="", llm_api_key="", llm_model="", parser_token="test")
    job = Job(id="0123456789abcdef", filename="example.pdf")
    asyncio.run(pipeline._parse(job, secrets))
    assert rows == events
    assert rows == [
        ("正在上传 1", 0.1),
        ("MinerU 已接收", 0.1),
        ("MinerU 正在解析 1", 0.4),
        ("MinerU 正在解析 3", 0.6),
        ("MinerU 正在解析完成", 1.0),
        ("MinerU 正在解析超时重试", 1.0),
        ("正在下载", 1.0),
    ]


def test_parse_cloud_queue_changes_are_persisted_and_published_without_throttling(tmp_path, monkeypatch):
    queue = "MinerU 已接收任务，正在等待解析资源（队列中）"
    transitions = [
        ("正在上传至 MinerU", False),
        ("MinerU 正在接收上传文件（waiting-file）", False),
        (queue, True),
        (queue, True),
        ("MinerU 正在接收上传文件（waiting-file）", False),
        (queue, True),
        ("MinerU 正在解析页面", False),
        (queue, True),
        ("MinerU 正在整理解析结果", False),
        ("正在下载解析结果", False),
        (queue, True),
    ]
    container = build_container(Settings(data_dir=tmp_path, layout_cache_ttl_hours=0))
    pipeline = container.pipeline
    job = Job(id="0123456789abcdef", filename="example.pdf", status=JobStatus.RUNNING)
    pipeline.deps.store.create_job(job)
    published = []
    monkeypatch.setattr(pipeline.deps.events, "publish", lambda current: published.append(current.model_copy()))

    class Parser:
        async def parse(self, pdf, pages, raw_dir, on_progress):
            for message, active in transitions:
                before = job.parse_queue_started_at
                on_progress(message)
                assert (job.parse_queue_started_at is not None) == active
                if active and before:
                    assert job.parse_queue_started_at == before
                stored = pipeline.deps.store.get_job(job.id)
                assert stored is not None and stored.parse_queue_started_at == job.parse_queue_started_at
            return LayoutResult(parser="mineru", pages=[])

    pipeline.deps = replace(pipeline.deps, parser_factory=lambda *_: Parser(), parse_retry_delays=(0,))
    secrets = Secrets(llm_base_url="", llm_api_key="", llm_model="", parser_token="test")
    try:
        asyncio.run(pipeline._parse(job, secrets))
        assert job.parse_queue_started_at is None
        assert any(JobOut.of(current).parse_queue_elapsed_s is not None for current in published)
        job.parse_queue_started_at = datetime.now(UTC) - timedelta(seconds=30)
        assert 29.9 <= (JobOut.of(job).parse_queue_elapsed_s or 0) <= 31
        pipeline._update(job, stage="fuse")
        assert job.parse_queue_started_at is None and JobOut.of(job).parse_queue_elapsed_s is None

        async def retry():
            job.stage = "parse"
            job.message = queue
            job.parse_queue_started_at = datetime.now(UTC)
            count = 0

            async def attempt():
                nonlocal count
                count += 1
                if count == 1:
                    raise ParseError("合成网络错误", retryable=True)
                assert job.parse_queue_started_at is None
                return LayoutResult(parser="mineru", pages=[])

            await pipeline._parse_with_retry(job, attempt)

        asyncio.run(retry())
    finally:
        container.close()


def test_paddle_keeps_jsonl_while_selected_pages_stay_in_the_pipeline(tmp_path):
    import json

    import pymupdf

    from pdftr.domain.job import JobOptions
    from pdftr.parsing.paddle_vl import PaddleVLParser

    class Paddle(PaddleVLParser):
        async def parse(self, pdf, pages=None, raw_dir=None, on_progress=None):
            assert pages is None
            return await super().parse(pdf, pages, raw_dir, on_progress)

        async def _run(self, pdf, progress):
            return json.dumps({"result": {"layoutParsingResults": [{"prunedResult": {"width": 595}}]}})

    pipeline = build_container(
        Settings(data_dir=tmp_path, layout_cache_ttl_hours=0),
        {"parser_factory": lambda *_: Paddle("test")},
    ).pipeline
    job = Job(id="0123456789abcdef", filename="example.pdf", options=JobOptions(parser="paddleocr-vl", pages=[0]))
    pdf = pipeline.deps.artifacts.path(job.id, "original.pdf")
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(pdf)
    asyncio.run(pipeline._parse(job, Secrets(llm_base_url="", llm_api_key="", llm_model="", parser_token="test")))
    assert (pdf.parent / "parser" / "paddleocr.jsonl").is_file()
    assert not (pdf.parent / "parser-tmp").exists()
