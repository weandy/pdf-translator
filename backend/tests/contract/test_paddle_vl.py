"""PaddleOCR-VL 适配器契约测试：流式分块上传、取消清理、有界流式下载及公网 HTTPS/防重定向约束。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pymupdf
import pytest

from pdftr.parsing import paddle_vl
from pdftr.parsing.paddle_vl import JOB_URL, PaddleVLParser
from pdftr.parsing.port import ParseError

RESULT_URL = "https://cdn.example/paddle-result.jsonl"


def _make_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((72, 72), "Synthetic Paddle PDF", fontsize=12)
    doc.save(path)
    doc.close()
    return path


def _sample_jsonl() -> bytes:
    line = json.dumps(
        {
            "result": {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "width": 600,
                            "parsing_res_list": [
                                {"block_label": "text", "block_bbox": [50, 50, 500, 100], "block_content": "Hello"}
                            ],
                        }
                    }
                ]
            }
        }
    )
    return (line + "\n").encode("utf-8")


def test_paddle_streams_upload_without_read_bytes_and_parses_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _make_pdf(tmp_path / "stream.pdf")
    monkeypatch.setattr(paddle_vl, "CHUNK", 128)
    pdf_bytes = pdf.read_bytes()

    def _forbid_read_bytes(self: Path) -> bytes:
        raise AssertionError("流式上传不得调用 Path.read_bytes() 将整份 PDF 载入内存")

    monkeypatch.setattr(Path, "read_bytes", _forbid_read_bytes)

    received_body = bytearray()
    received_content_length: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if request.method == "POST" and url == JOB_URL:
            received_content_length.append(request.headers["content-length"])
            data = await request.aread()
            received_body.extend(data)
            return httpx2.Response(200, json={"code": 0, "data": {"jobId": "pj-1"}})
        if request.method == "GET" and url == f"{JOB_URL}/pj-1":
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": RESULT_URL}}},
            )
        if request.method == "GET" and url == RESULT_URL:
            return httpx2.Response(200, content=_sample_jsonl())
        return httpx2.Response(404)

    parser = PaddleVLParser("ptok", poll_interval=0, retry_delays=(), transport=httpx2.MockTransport(handler))
    result = asyncio.run(parser.parse(pdf))
    assert len(result.pages) == 1
    assert received_content_length == [str(len(received_body))]
    assert pdf_bytes in bytes(received_body)


def test_paddle_upload_cancel_waits_for_task_and_releases_file_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _make_pdf(tmp_path / "cancel.pdf")
    monkeypatch.setattr(paddle_vl, "CHUNK", 64)

    async def scenario() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def slow_post(url: str, *, content, headers):
            del url, headers
            assert await anext(content)
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async with httpx2.AsyncClient() as client:
            monkeypatch.setattr(client, "post", slow_post)
            parser = PaddleVLParser("ptok", retry_delays=())
            task = asyncio.create_task(parser._submit(client, pdf, lambda message, fraction=None: None))
            await asyncio.wait_for(started.wait(), 1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleaned.is_set()
        # Windows 下文件句柄已完全关闭可立即删除
        pdf.unlink()

    asyncio.run(scenario())


@pytest.mark.parametrize("advertised", ["128", "1"])
def test_paddle_download_enforces_max_result_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, advertised: str
) -> None:
    monkeypatch.setattr(paddle_vl, "MAX_RESULT_BYTES", 64)
    pdf = _make_pdf(tmp_path / "big.pdf")

    class _OversizedJsonl(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 70

    async def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if request.method == "POST" and url == JOB_URL:
            await request.aread()
            return httpx2.Response(200, json={"code": 0, "data": {"jobId": "pj-big"}})
        if request.method == "GET" and url == f"{JOB_URL}/pj-big":
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": RESULT_URL}}},
            )
        if request.method == "GET" and url == RESULT_URL:
            return httpx2.Response(200, headers={"Content-Length": advertised}, stream=_OversizedJsonl())
        return httpx2.Response(404)

    parser = PaddleVLParser("ptok", poll_interval=0, retry_delays=(), transport=httpx2.MockTransport(handler))
    with pytest.raises(ParseError, match="返回结果过大"):
        asyncio.run(parser.parse(pdf))


def test_paddle_download_rejects_plaintext_http_and_redirects(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path / "sec.pdf")

    # 明文 http:// 结果地址直接拦截
    async def http_result(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if request.method == "POST" and url == JOB_URL:
            await request.aread()
            return httpx2.Response(200, json={"code": 0, "data": {"jobId": "pj-http"}})
        if request.method == "GET" and url == f"{JOB_URL}/pj-http":
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": "http://cdn.example/r.jsonl"}}},
            )
        pytest.fail("不应访问明文 http 下载地址")

    p_http = PaddleVLParser("ptok", poll_interval=0, retry_delays=(), transport=httpx2.MockTransport(http_result))
    with pytest.raises(ParseError, match="https"):
        asyncio.run(p_http.parse(pdf))

    # 302 跳转直接拒绝，绝不跟随
    followed: list[str] = []

    async def redir_result(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if request.method == "POST" and url == JOB_URL:
            await request.aread()
            return httpx2.Response(200, json={"code": 0, "data": {"jobId": "pj-302"}})
        if request.method == "GET" and url == f"{JOB_URL}/pj-302":
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": RESULT_URL}}},
            )
        if request.method == "GET" and url == RESULT_URL:
            return httpx2.Response(302, headers={"location": "https://other.example/next.jsonl"})
        followed.append(url)
        return httpx2.Response(200, content=_sample_jsonl())

    p_redir = PaddleVLParser("ptok", poll_interval=0, retry_delays=(), transport=httpx2.MockTransport(redir_result))
    with pytest.raises(ParseError, match="重定向"):
        asyncio.run(p_redir.parse(pdf))
    assert followed == []
