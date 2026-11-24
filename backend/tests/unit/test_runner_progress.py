import asyncio
from types import SimpleNamespace

from pdftr.config import Settings
from pdftr.container import build_container
from pdftr.domain.job import Job
from pdftr.domain.layout import LayoutResult
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


def test_paddle_keeps_jsonl_while_selected_pages_stay_in_the_pipeline(tmp_path):
    import json

    import pymupdf

    from pdftr.domain.job import JobOptions
    from pdftr.parsing.paddle_vl import PaddleVLParser

    class Paddle(PaddleVLParser):
        async def parse(self, pdf, pages=None, raw_dir=None, on_progress=None):
            assert pages is None
            return await super().parse(pdf, pages, raw_dir, on_progress)

        async def _run(self, data, name, progress):
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
