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

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.seen.append(f"{request.method} {url}")
        if url.startswith(API):
            assert request.headers["Authorization"] == "Bearer tok"
            if not self.auth_ok:
                return httpx2.Response(401)
        if request.method == "POST" and url == f"{API}/file-urls/batch":
            body = await request.aread()
            assert b'"enable_formula": true' in body or b'"enable_formula":true' in body
            return httpx2.Response(200, json={"code": 0, "data": {"batch_id": "b1", "file_urls": [UPLOAD]}})
        if request.method == "PUT" and url == UPLOAD:
            assert "Authorization" not in request.headers  # 预签名地址不能带鉴权头
            assert await request.aread() == b"%PDF-fake"
            assert request.headers["content-length"] == "9"  # 显式长度，不用分块传输编码
            return httpx2.Response(200)
        if request.method == "GET" and url == f"{API}/extract-results/batch/b1":
            self.polls += 1
            if self.fail:
                return httpx2.Response(
                    200, json={"code": 0, "data": {"extract_result": [{"state": "failed", "err_msg": "坏文件"}]}}
                )
            state = ["pending", "running", "done"][min(self.polls - 1, 2)]
            progress = {"extracted_pages": 1, "total_pages": 2} if state == "running" else {}
            item = {"state": state, "full_zip_url": ZIP, "extract_progress": progress}
            return httpx2.Response(200, json={"code": 0, "data": {"extract_result": [item]}})
        if request.method == "GET" and url == ZIP:
            return httpx2.Response(200, content=_zip(), headers={"content-type": "application/zip"})
        return httpx2.Response(404)


def _parser(fake: FakeMinerU) -> MinerUCloudParser:
    return MinerUCloudParser("tok", poll_interval=0, transport=httpx2.MockTransport(fake))


def test_full_flow(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = FakeMinerU()
    seen: list[tuple[str, float | None]] = []

    def on_progress(message: str, fraction: float | None = None) -> None:
        seen.append((message, fraction))

    result = asyncio.run(_parser(fake).parse(pdf, raw_dir=tmp_path / "raw", on_progress=on_progress))
    assert len(result.pages) == 2
    assert result.parser_version == "hybrid/3.4.4"
    assert fake.polls == 3
    # 排队、逐页解析的状态都转告给用户
    assert seen[0][0] == "正在上传到 MinerU 云端"
    # 上传结果（大小、用时、速度）带进后续状态里一直显示
    assert seen[1][0].startswith("MinerU 已接收任务，正在等待解析资源（队列中）（已上传 0.0 MB，用时")
    assert seen[2][0].startswith("MinerU 正在解析，第 1/2 页（50%）（已上传")
    assert seen[2][1] == 0.5
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


def test_cloud_timeout_is_retryable_and_reports_resubmission():
    async def pending(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {"extract_result": [{"state": "pending"}]},
            },
        )

    parser = MinerUCloudParser("tok", timeout=0.01, poll_interval=0, transport=httpx2.MockTransport(pending))
    messages: list[str] = []

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(pending)) as client:
            with pytest.raises(ParseError, match="超过 5 分钟") as error:
                await parser._wait(
                    client,
                    "batch",
                    lambda message, fraction=None: messages.append(message),
                )
            assert error.value.retryable

    asyncio.run(run())
    assert messages[-1] == "MinerU 云端解析等待超过 5 分钟，准备重新提交"


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


def test_stalled_request_hits_deadline_and_retries():
    """网络时断时续时 httpx 的单次读写超时不会触发，要靠总时限（实测上传曾卡住 7 分钟以上）。"""
    calls = []

    async def stall(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.method)
        await asyncio.sleep(5)
        return httpx2.Response(200)

    parser = MinerUCloudParser("tok", retry_delays=(0,), transport=httpx2.MockTransport(stall))

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(stall)) as client:
            await parser._send(client, "PUT", "https://upload.example/x", content=b"x", deadline=0.05)

    with pytest.raises(httpx2.TimeoutException, match="超过 0 秒未完成"):
        asyncio.run(run())
    assert calls == ["PUT"]  # deadline 覆盖内部重试，不能重复累加请求时限


def test_upload_status_text():
    from pdftr.parsing.mineru_cloud import upload_status

    assert upload_status(1258291, 3565158, 40) == "正在上传到 MinerU 云端：1.2 / 3.4 MB，31 KB/s，约 1 分 13 秒"
    assert upload_status(0, 3565158, 0.5) == "正在上传到 MinerU 云端：0.0 / 3.4 MB"


def test_proxy_setting_reaches_parser(tmp_path: Path):
    from pdftr.config import Settings
    from pdftr.container import build_container
    from pdftr.pipeline.secrets import Secrets

    settings = Settings(data_dir=tmp_path, glossary_sources="", mineru_proxy="socks5h://u:p@127.0.0.1:1080")
    deps = build_container(settings).pipeline.deps
    secrets = Secrets(llm_base_url="https://api.deepseek.com", llm_api_key="k", llm_model="m", parser_token="t")
    parser = deps.parser_factory(secrets, "mineru-cloud")
    assert isinstance(parser, MinerUCloudParser) and parser._proxy == "socks5h://u:p@127.0.0.1:1080"
    from pdftr.parsing.paddle_vl import PaddleVLParser

    paddle = deps.parser_factory(Secrets(**{**secrets.__dict__, "paddle_token": "pt"}), "paddleocr-vl")
    assert isinstance(paddle, PaddleVLParser) and paddle._proxy is None  # 默认不走代理


def test_upload_reopens_path_on_retry_and_joins_cancelled_request(tmp_path, monkeypatch):
    from pdftr.parsing import mineru_cloud

    monkeypatch.setattr(mineru_cloud, "UPLOAD_CHUNK", 4)
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-content")

    class Client:
        def __init__(self):
            self.first_chunks = []
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        async def request(self, method, url, *, content, headers):
            assert method == "PUT" and headers == {"Content-Length": "12"}
            self.first_chunks.append(await anext(content))
            if len(self.first_chunks) == 1:
                raise httpx2.WriteError("synthetic failure")
            self.started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                self.closed.set()

    async def run():
        client = Client()
        parser = MinerUCloudParser("tok", retry_delays=(0,))
        async with httpx2.AsyncClient() as http:
            monkeypatch.setattr(http, "request", client.request)
            task = asyncio.create_task(parser._upload(http, UPLOAD, pdf, lambda *_: None))
            try:
                await asyncio.wait_for(client.started.wait(), 1)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert client.closed.is_set()
            assert client.first_chunks == [b"%PDF", b"%PDF"]
        # Windows 上也必须已经释放句柄。
        pdf.unlink()

    asyncio.run(run())


def test_streamed_zip_enforces_layout_limit_and_cleans_up(tmp_path, monkeypatch):
    from pdftr.parsing import mineru_cloud

    class ZipStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            payload = _zip()
            for offset in range(0, len(payload), 31):
                yield payload[offset : offset + 31]

    fake = FakeMinerU()

    async def handler(request):
        if str(request.url) == ZIP:
            return httpx2.Response(200, stream=ZipStream())
        return await fake(request)

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    parser = MinerUCloudParser("tok", poll_interval=0, transport=httpx2.MockTransport(handler))
    raw = tmp_path / "raw"
    monkeypatch.setattr(mineru_cloud, "MAX_LAYOUT_BYTES", 30, raising=False)
    with pytest.raises(ParseError, match=r"layout\.json.*超过"):
        asyncio.run(parser.parse(pdf, raw_dir=raw))
    assert not list(raw.iterdir())
