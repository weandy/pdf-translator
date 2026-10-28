"""MinerU 客户端契约测试：用假传输层模拟官方 API 的完整流程，不访问网络。"""

import asyncio
import io
import zipfile
from pathlib import Path

import httpx2
import pytest

from pdftr.parsing.mineru_cloud import API, MinerUCloudParser
from pdftr.parsing.port import ParseError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "mineru_layout_min.json"
UPLOAD = "https://oss.example/upload?sig=1"
ZIP = "https://cdn.example/result.zip"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("abc/layout.json", FIXTURE.read_text(encoding="utf-8"))
        z.writestr("abc/full.md", "# ignored")
    return buf.getvalue()


class FakeMinerU:
    def __init__(self, *, auth_ok: bool = True, fail: bool = False) -> None:
        self.auth_ok, self.fail = auth_ok, fail
        self.polls = 0
        self.seen: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.seen.append(f"{request.method} {url}")
        if url.startswith(API):
            assert request.headers["Authorization"] == "Bearer tok"
            if not self.auth_ok:
                return httpx2.Response(401)
        if request.method == "POST" and url == f"{API}/file-urls/batch":
            body = request.read()
            assert b'"enable_formula": true' in body or b'"enable_formula":true' in body
            return httpx2.Response(200, json={"code": 0, "data": {"batch_id": "b1", "file_urls": [UPLOAD]}})
        if request.method == "PUT" and url == UPLOAD:
            assert "Authorization" not in request.headers  # 预签名地址不能带鉴权头
            assert request.read() == b"%PDF-fake"
            return httpx2.Response(200)
        if request.method == "GET" and url == f"{API}/extract-results/batch/b1":
            self.polls += 1
            if self.fail:
                return httpx2.Response(
                    200, json={"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "坏文件"}]}}
                )
            state = "running" if self.polls < 2 else "done"
            return httpx2.Response(
                200, json={"code": 0, "data": {"extract_result": [{"state": state, "full_zip_url": ZIP}]}}
            )
        if request.method == "GET" and url == ZIP:
            return httpx2.Response(200, content=_zip(), headers={"content-type": "application/zip"})
        return httpx2.Response(404)


def _parser(fake: FakeMinerU) -> MinerUCloudParser:
    return MinerUCloudParser("tok", poll_interval=0, transport=httpx2.MockTransport(fake))


def test_full_flow(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = FakeMinerU()
    result = asyncio.run(_parser(fake).parse(pdf, raw_dir=tmp_path / "raw"))
    assert len(result.pages) == 2
    assert result.parser_version == "hybrid/3.4.4"
    assert fake.polls == 2
    assert (tmp_path / "raw" / "mineru.zip").exists()


@pytest.mark.parametrize(
    ("fake", "message"),
    [(FakeMinerU(auth_ok=False), "Token 无效"), (FakeMinerU(fail=True), "坏文件")],
)
def test_errors_are_user_facing(tmp_path: Path, fake: FakeMinerU, message: str):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    with pytest.raises(ParseError, match=message):
        asyncio.run(_parser(fake).parse(pdf))


def test_missing_token():
    with pytest.raises(ParseError, match="未配置"):
        MinerUCloudParser("")


def test_network_errors_are_user_facing(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("timed out", request=request)

    for handler, message in [(refuse, "无法连接 MinerU"), (slow, "超时")]:
        parser = MinerUCloudParser("tok", retry_delays=(0, 0), transport=httpx2.MockTransport(handler))
        with pytest.raises(ParseError, match=message):
            asyncio.run(parser.parse(pdf))


def test_transient_network_error_is_retried(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    calls = []

    def flaky(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx2.ConnectTimeout("timed out", request=request)
        return httpx2.Response(401)  # 第二次请求到达了服务器

    parser = MinerUCloudParser("tok", retry_delays=(0,), transport=httpx2.MockTransport(flaky))
    with pytest.raises(ParseError, match="Token 无效"):
        asyncio.run(parser.parse(pdf))
    assert calls == ["POST", "POST"]
