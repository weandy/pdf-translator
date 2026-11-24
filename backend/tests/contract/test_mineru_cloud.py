"""MinerU 客户端契约测试：用假传输层模拟官方 API 的完整流程，不访问网络。"""

import asyncio
import io
import threading
import zipfile
from pathlib import Path
from time import monotonic

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
            task = asyncio.create_task(parser._upload(http, UPLOAD, pdf, lambda message, fraction=None: None))
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


def test_upload_waits_for_put_cleanup_even_after_second_cancel(tmp_path, monkeypatch):
    pdf = tmp_path / "upload.pdf"
    pdf.write_bytes(b"%PDF-synthetic")

    async def run():
        started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleanup_complete = False

        async def put(method, url, *, content, headers):
            nonlocal cleanup_complete
            assert await anext(content) == b"%PDF-synthetic"
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await finish.wait()
                cleanup_complete = True

        async with httpx2.AsyncClient() as client:
            monkeypatch.setattr(client, "request", put)
            parser = MinerUCloudParser("tok")
            upload = asyncio.create_task(parser._upload(client, UPLOAD, pdf, lambda message, fraction=None: None))
            try:
                await asyncio.wait_for(started.wait(), 1)
                upload.cancel()
                await asyncio.wait_for(cleaning.wait(), 1)
                upload.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
                assert not upload.done()
            finally:
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(upload, 1)
            assert cleanup_complete
        pdf.unlink()

    asyncio.run(run())


@pytest.mark.parametrize(("deadline_delta", "reason"), [(0.02, "总时限"), (10.0, "没有进展")])
def test_upload_distinguishes_total_timeout_and_stalled_transfer(tmp_path, monkeypatch, deadline_delta, reason):
    pdf = tmp_path / "upload.pdf"
    pdf.write_bytes(b"%PDF-synthetic")

    async def stall(*args, **kwargs):
        await asyncio.Future()

    async def run():
        async with httpx2.AsyncClient() as client:
            monkeypatch.setattr(client, "request", stall)
            parser = MinerUCloudParser("tok", retry_delays=(), stall_timeout=0.05)
            with pytest.raises(ParseError, match=reason) as exc:
                await parser._upload(
                    client, UPLOAD, pdf, lambda message, fraction=None: None, deadline_at=monotonic() + deadline_delta
                )
            assert exc.value.retryable

    asyncio.run(run())


async def _download_with(handler, raw, *, delays=()):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        parser = MinerUCloudParser("tok", retry_delays=delays)
        return await parser._download(client, ZIP, raw, monotonic() + 10)


@pytest.mark.parametrize("advertised", ["65", "1", "invalid"])
def test_download_caps_archive_from_header_and_stream(tmp_path, monkeypatch, advertised):
    from pdftr.parsing import mineru_cloud

    monkeypatch.setattr(mineru_cloud, "MAX_ARCHIVE_BYTES", 64)
    reads = []

    class Oversized(httpx2.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            yield b"x" * 65

    def handler(request):
        return httpx2.Response(200, headers={"Content-Length": advertised}, stream=Oversized())

    with pytest.raises(ParseError, match="ZIP 超过大小上限"):
        asyncio.run(_download_with(handler, tmp_path))
    assert reads == ([] if advertised == "65" else [True])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("entries", "message"),
    [(None, "ZIP 或 layout"), ({"other.json": "{}"}, "缺少 layout.json")],
)
def test_download_rejects_invalid_or_missing_layout_zip(tmp_path, entries, message):
    data = io.BytesIO()
    if entries is None:
        data.write(b"synthetic invalid archive")
    else:
        with zipfile.ZipFile(data, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value)
    with pytest.raises(ParseError, match=message):
        asyncio.run(_download_with(lambda _: httpx2.Response(200, content=data.getvalue()), tmp_path))
    assert not list(tmp_path.iterdir())


def test_download_checks_actual_layout_size_despite_small_zip_metadata(tmp_path, monkeypatch):
    from pdftr.parsing import mineru_cloud

    # 模拟条目声明很小但读取超限，仅用几十字节触发运行时保护。
    monkeypatch.setattr(mineru_cloud, "MAX_LAYOUT_BYTES", 32)
    payload = _zip()
    infolist = zipfile.ZipFile.infolist

    def understated_info(archive):
        for info in infolist(archive):
            if info.filename.endswith("layout.json"):
                info.file_size = 1
        return infolist(archive)

    def oversized_entry(archive, info):
        assert info.file_size == 1
        return io.BytesIO(b"x" * 33)

    monkeypatch.setattr(zipfile.ZipFile, "infolist", understated_info)
    monkeypatch.setattr(zipfile.ZipFile, "open", oversized_entry)
    with pytest.raises(ParseError, match="解压后超过"):
        asyncio.run(_download_with(lambda _: httpx2.Response(200, content=payload), tmp_path))
    assert not list(tmp_path.iterdir())


def test_download_truncates_partial_archive_before_retry(tmp_path, monkeypatch):
    from pdftr.parsing import mineru_cloud

    payload = _zip()
    monkeypatch.setattr(mineru_cloud, "UPLOAD_CHUNK", 32)
    calls = []

    class Interrupted(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (len(payload) + 256)
            raise httpx2.ReadError("synthetic interruption")

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx2.Response(200, stream=Interrupted())
        return httpx2.Response(200, content=payload)

    result = asyncio.run(_download_with(handler, tmp_path, delays=(0,)))
    assert len(result.pages) == 2 and calls == ["GET", "GET"]
    assert (tmp_path / "mineru.zip").read_bytes() == payload
    assert not list(tmp_path.glob("*.part"))


def test_cancelled_download_keeps_zip_until_normalization_thread_exits(tmp_path, monkeypatch):
    from pdftr.parsing import mineru_cloud

    path = None
    reading, release = threading.Event(), threading.Event()
    read_layout = mineru_cloud._read_layout
    payload = _zip()

    def read(archive):
        nonlocal path
        path = archive
        with archive.open("rb") as source:
            reading.set()
            assert release.wait(2)
            assert source.read(2) == b"PK"
        return read_layout(archive)

    monkeypatch.setattr(mineru_cloud, "_read_layout", read)

    async def run():
        download = asyncio.create_task(_download_with(lambda _: httpx2.Response(200, content=payload), tmp_path))
        try:
            assert await asyncio.to_thread(reading.wait, 1)
            download.cancel()
            await asyncio.sleep(0)
            download.cancel()
            await asyncio.sleep(0)
            assert not download.done()
            assert path is not None and path.exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(download, 2)

    asyncio.run(run())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("interval", "first_wait", "fast", "slow"),
    [
        (None, 4.0, 1.0, 3.0),
        (0, None, 0, 0),
        (2.0, None, 2.0, 2.0),
    ],
)
def test_poll_schedule(monkeypatch, interval, first_wait, fast, slow):
    from types import SimpleNamespace

    import pdftr.parsing.mineru_cloud as cloud

    now = 100.0
    sleeps = []

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    parser = MinerUCloudParser("tok", poll_interval=interval)
    monkeypatch.setattr(cloud, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(cloud, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(cloud.random, "uniform", lambda *_: 0)

    async def run():
        await parser._wait_before_first_poll(100.0, 200.0)
        assert sleeps == ([] if first_wait is None else [first_wait])
        await parser._wait_before_first_poll(90.0, 200.0)
        assert sleeps == ([] if first_wait is None else [first_wait])
        idle = await parser._wait_before_next_poll(False, None, 200.0)
        assert sleeps[-1] == fast
        await parser._wait_before_next_poll(False, now - 30.0, 200.0)
        assert sleeps[-1] == slow
        assert await parser._wait_before_next_poll(True, idle, 200.0) is None
        assert sleeps[-1] == fast
        await parser._wait_before_next_poll(False, now - 30.0, now + 0.25)
        assert sleeps[-1] == min(slow, 0.25)

    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403])
def test_presigned_auth_errors_are_retryable_and_redacted(status):
    from pdftr.parsing.mineru_cloud import _check

    response = httpx2.Response(status, json={"msg": UPLOAD})
    with pytest.raises(ParseError, match="临时传输链接") as error:
        _check(response, "下载", presigned=True)
    assert error.value.retryable
    assert UPLOAD not in str(error.value)
    with pytest.raises(ParseError, match="Token 无效") as error:
        _check(response, "查询")
    assert not error.value.retryable


def test_same_volume_attempt_removed_after_parse(tmp_path):
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = FakeMinerU()

    async def handler(request):
        attempts = list((tmp_path / "parser-tmp").glob("attempt-*"))
        assert len(attempts) == 1
        if str(request.url) == ZIP:
            assert len(list(attempts[0].glob("*.part"))) == 1
        return await fake(request)

    parser = MinerUCloudParser("tok", poll_interval=0, use_pdf_parent_tmp=True, transport=httpx2.MockTransport(handler))
    assert len(asyncio.run(parser.parse(pdf)).pages) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["original.pdf"]


class FakeShardedMinerU:
    def __init__(self, *, fail_first=False, always_fail=False, saturation=None):
        self.batches = {}
        self.uploads = {}
        self.active = {"PUT": 0, "GET": 0}
        self.peak = {"PUT": 0, "GET": 0}
        self.by_batch = {}
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.downloaded = []
        self.saturation = saturation or {}
        self.saturated = {method: asyncio.Event() for method in self.saturation}

    async def __call__(self, request):
        import json

        import pymupdf

        url = request.url
        key = url.path.rsplit("/", 1)[-1]
        if request.method == "POST":
            key = str(len(self.batches))
            files = json.loads(await request.aread())["files"]
            assert all(item["name"].startswith("doc_") for item in files)
            self.batches[key] = files
            links = [f"https://oss.example/{key}_{i}" for i in range(len(files))]
            return httpx2.Response(200, json={"data": {"batch_id": key, "file_urls": links}})
        if url.host == "mineru.net":
            items = []
            for i, item in reversed(list(enumerate(self.batches[key]))):
                index = int(item["name"].removeprefix("doc_").removesuffix(".pdf"))
                fail = index == 1 and (self.always_fail or (self.fail_first and key == "0"))
                items.append(
                    {
                        "data_id": item["data_id"],
                        "file_name": item["name"],
                        "state": "failed" if fail else "done",
                        "full_zip_url": f"https://cdn.example/{key}_{i}",
                        "err_msg": "busy https://private.example/path?sig=secret",
                    }
                )
            return httpx2.Response(200, json={"data": {"extract_result": items}})
        method = request.method
        self.active[method] += 1
        self.peak[method] = max(self.peak[method], self.active[method])
        batch_counter = (method, key.split("_", 1)[0])
        self.by_batch[batch_counter] = self.by_batch.get(batch_counter, 0) + 1
        try:
            assert self.by_batch[batch_counter] <= 2
            if method in self.saturation:
                if self.active[method] >= self.saturation[method]:
                    self.saturated[method].set()
                await self.saturated[method].wait()
            await asyncio.sleep(0.015)
            if method == "PUT":
                with pymupdf.open(stream=await request.aread(), filetype="pdf") as doc:
                    self.uploads[key] = doc.page_count
                return httpx2.Response(200)
            self.downloaded.append(key)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                infos = [{"page_idx": i, "page_size": [595, 842]} for i in range(self.uploads[key])]
                z.writestr("layout.json", json.dumps({"pdf_info": infos}))
            return httpx2.Response(200, content=stream.getvalue())
        finally:
            self.active[method] -= 1
            self.by_batch[batch_counter] -= 1


def _shard_job(root, *, pages=52):
    import pymupdf

    root.mkdir()
    pdf = root / "original.pdf"
    with pymupdf.open() as doc:
        for _ in range(pages):
            doc.new_page()
        doc.save(pdf)
    return pdf


def test_shard_transfer_and_local_resource_limits(tmp_path):
    from contextlib import asynccontextmanager

    from pdftr.parsing.mineru_cloud import MinerUResourceLimits

    fake = FakeShardedMinerU(saturation={"PUT": 4, "GET": 2})

    async def run():
        local = asyncio.Semaphore(1)
        active = 0
        calls = 0

        @asynccontextmanager
        async def local_slot():
            nonlocal active, calls
            async with local:
                active += 1
                calls += 1
                assert active == 1
                try:
                    yield
                finally:
                    active -= 1

        limits = MinerUResourceLimits(local_slot=local_slot)
        jobs = [_shard_job(tmp_path / str(i)) for i in range(4)]
        parsers = [
            MinerUCloudParser(
                "tok",
                poll_interval=0,
                enable_sharding=True,
                use_pdf_parent_tmp=True,
                limits=limits,
                transport=httpx2.MockTransport(fake),
            )
            for _ in jobs
        ]
        async with asyncio.timeout(5):
            results = await asyncio.gather(*(p.parse(job) for p, job in zip(parsers, jobs, strict=True)))
        assert all([page.index for page in result.pages] == list(range(52)) for result in results)
        assert calls == 20 and active == 0
        assert all(not (job.parent / "parser-tmp").exists() for job in jobs)

    asyncio.run(run())
    assert fake.peak == {"PUT": 4, "GET": 2}


@pytest.mark.parametrize("always_fail", [False, True])
def test_only_failed_shard_is_resubmitted(tmp_path, always_fail, monkeypatch):
    sleep = asyncio.sleep

    async def fast_sleep(delay):
        await sleep(0 if delay == 3.0 else delay)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    fake = FakeShardedMinerU(fail_first=True, always_fail=always_fail)
    pdf = _shard_job(tmp_path / "job")
    parser = MinerUCloudParser(
        "tok", poll_interval=0, enable_sharding=True, use_pdf_parent_tmp=True, transport=httpx2.MockTransport(fake)
    )
    if always_fail:
        with pytest.raises(ParseError, match="分片解析失败") as error:
            asyncio.run(parser.parse(pdf))
        assert error.value.retryable and error.value.max_retries == 1
        assert "secret" not in str(error.value)
    else:
        assert [page.index for page in asyncio.run(parser.parse(pdf)).pages] == list(range(52))
    assert [len(files) for files in fake.batches.values()] == [4, 1]
    assert fake.batches["1"][0]["name"] == "doc_1.pdf"
    assert set(fake.downloaded) == {"0_0", "0_2", "0_3"} | (set() if always_fail else {"1_0"})
    assert not (pdf.parent / "parser-tmp").exists()


@pytest.mark.parametrize("case", ["duplicate", "foreign", "missing_id", "name"])
def test_batch_rejects_ambiguous_results(tmp_path, case):
    from pdftr.parsing.mineru_cloud import _align_batch, _RemoteShard
    from pdftr.parsing.mineru_plan import ShardSpec

    batch = {
        name: _RemoteShard(ShardSpec(i, (i,), False), tmp_path / name, f"doc_{i}.pdf", name)
        for i, name in enumerate(("first", "second"))
    }
    bad = {
        "duplicate": {"data_id": "first"},
        "foreign": {"data_id": "foreign"},
        "missing_id": {},
        "name": {"data_id": "second", "file_name": "doc_0.pdf"},
    }
    with pytest.raises(ParseError, match="批次项"):
        _align_batch([{"data_id": "first"}, bad[case]], batch)


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1", "https://[::1]", "https://host.local"])
def test_invalid_presigned_hosts_are_redacted(url):
    from pdftr.parsing.mineru_cloud import _presigned_host

    with pytest.raises(ParseError, match="不安全") as error:
        _presigned_host(url)
    assert url not in str(error.value)


@pytest.mark.parametrize("stage", ["plan", "put", "get", "normalize"])
@pytest.mark.parametrize("repeat", [False, True])
def test_sharded_cancel_joins_before_local_and_transfer_cleanup(tmp_path, monkeypatch, stage, repeat):
    import pdftr.parsing.mineru_cloud as cloud

    pdf = _shard_job(tmp_path / "job")
    fake = FakeShardedMinerU()

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        thread_release = threading.Event()
        loop = asyncio.get_running_loop()

        def gate_thread(original):
            def gated(*args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                assert thread_release.wait(2)
                return original(*args, **kwargs)

            return gated

        if stage == "plan":
            monkeypatch.setattr(cloud, "materialize_shards", gate_thread(cloud.materialize_shards))
        elif stage == "normalize":
            monkeypatch.setattr(cloud, "_normalize_archive", gate_thread(cloud._normalize_archive))

        async def handler(request):
            if (request.method, request.url.host) == (
                ("PUT", "oss.example") if stage == "put" else ("GET", "cdn.example") if stage == "get" else ("", "")
            ):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    await release.wait()
            return await fake(request)

        limits = cloud.MinerUResourceLimits()
        parser = MinerUCloudParser(
            "tok",
            poll_interval=0,
            enable_sharding=True,
            use_pdf_parent_tmp=True,
            limits=limits,
            transport=httpx2.MockTransport(handler),
        )
        task = asyncio.create_task(parser.parse(pdf))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            if repeat:
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
            assert (pdf.parent / "parser-tmp").exists()
        finally:
            thread_release.set()
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not (pdf.parent / "parser-tmp").exists()
        async with asyncio.timeout(1):
            async with limits.local_slot():
                for semaphore, count in [(limits.global_uploads, 4), (limits.global_downloads, 2)]:
                    for _ in range(count):
                        await semaphore.acquire()
                    for _ in range(count):
                        semaphore.release()

    asyncio.run(run())


def test_total_layout_budget_counts_every_shard(tmp_path, monkeypatch):
    import pdftr.parsing.mineru_cloud as cloud

    pdf = _shard_job(tmp_path / "job")
    fake = FakeShardedMinerU()
    monkeypatch.setattr(cloud, "MAX_TOTAL_LAYOUT_BYTES", 2000)
    parser = MinerUCloudParser(
        "tok", poll_interval=0, enable_sharding=True, use_pdf_parent_tmp=True, transport=httpx2.MockTransport(fake)
    )
    with pytest.raises(ParseError, match="累计解压"):
        asyncio.run(parser.parse(pdf))
    assert not (pdf.parent / "parser-tmp").exists()


def test_native_ranges_submits_original_pdf_and_remaps_response(tmp_path):
    import json

    pdf = _shard_job(tmp_path / "job")
    pages = list(range(0, 16, 2))
    files = []

    async def handler(request):
        if request.method == "POST":
            files.extend(json.loads(await request.aread())["files"])
            return httpx2.Response(200, json={"data": {"batch_id": "b", "file_urls": [UPLOAD]}})
        if request.method == "PUT":
            assert await request.aread() == pdf.read_bytes()
            return httpx2.Response(200)
        if request.url.host == "mineru.net":
            result = {"state": "done", "data_id": files[0]["data_id"], "full_zip_url": ZIP}
            return httpx2.Response(200, json={"data": {"extract_result": [result]}})
        return httpx2.Response(200, content=_zip())

    parser = MinerUCloudParser(
        "tok", poll_interval=0, enable_partial_pages=True, transport=httpx2.MockTransport(handler)
    )
    result = asyncio.run(parser.parse(pdf, pages=pages))
    assert files[0]["page_ranges"] == "1,3,5,7,9,11,13,15"
    assert result.requested_pages == pages
    assert [p.index for p in result.pages] == [0, 2]


@pytest.mark.parametrize("urls", [[], [UPLOAD], [UPLOAD, UPLOAD], [UPLOAD, None]])
def test_batch_rejects_wrong_or_duplicate_upload_links(tmp_path, urls):
    pdf = _shard_job(tmp_path / "job")

    def handler(request):
        assert request.method == "POST"
        return httpx2.Response(200, json={"data": {"batch_id": "b", "file_urls": urls}})

    parser = MinerUCloudParser(
        "tok", poll_interval=0, enable_partial_pages=True, transport=httpx2.MockTransport(handler)
    )
    with pytest.raises(ParseError, match="上传链接返回格式异常"):
        asyncio.run(parser.parse(pdf, pages=[9, 10, 11]))


def test_missing_batch_item_cannot_publish_a_result(tmp_path, monkeypatch):
    fake = FakeShardedMinerU()
    pdf = _shard_job(tmp_path / "job")

    async def handler(request):
        response = await fake(request)
        if request.url.host == "mineru.net" and request.method == "GET":
            body = response.json()
            body["data"]["extract_result"].pop()
            return httpx2.Response(200, json=body)
        return response

    async def stop_polling(*args):
        raise ParseError("不完整批次")

    parser = MinerUCloudParser("tok", poll_interval=0, enable_sharding=True, transport=httpx2.MockTransport(handler))
    monkeypatch.setattr(parser, "_wait_before_next_poll", stop_polling)
    with pytest.raises(ParseError, match="不完整批次"):
        asyncio.run(parser.parse(pdf))
    assert fake.downloaded == []


def test_cli_long_pdf_keeps_original_zip_and_can_fuse(tmp_path, monkeypatch, caplog):
    import json
    import logging

    from pdftr import cli

    pdf = _shard_job(tmp_path / "job", pages=64)
    out = tmp_path / "out"

    async def handler(request):
        if request.method == "POST":
            payload = json.loads(await request.aread())
            assert payload["files"] == [{"name": "original.pdf", "data_id": "original"}]
            return httpx2.Response(200, json={"data": {"batch_id": "b", "file_urls": [UPLOAD]}})
        if request.method == "PUT":
            assert await request.aread() == pdf.read_bytes()
            return httpx2.Response(200)
        if request.url.host == "mineru.net":
            return httpx2.Response(200, json={"data": {"extract_result": [{"state": "done", "full_zip_url": ZIP}]}})
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            infos = [{"page_idx": p, "page_size": [595, 842]} for p in range(64)]
            archive.writestr("layout.json", json.dumps({"pdf_info": infos}))
        return httpx2.Response(200, content=content.getvalue())

    def factory(*args, **kwargs):
        return MinerUCloudParser("tok", poll_interval=0, transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(cli, "MinerUCloudParser", factory)
    with caplog.at_level(logging.INFO):
        cli.main(["parse", str(pdf), "--out", str(out)])
    assert sorted(p.name for p in out.iterdir()) == ["layout.json", "mineru.zip"]
    assert "?sig=1" not in caplog.text and ZIP not in caplog.text
    assert "mode=full_single pages=64 shards=1" in caplog.text
    cli.main(["fuse", str(pdf), str(out / "layout.json"), "--out", str(out / "ir.json")])
    assert len(json.loads((out / "ir.json").read_text(encoding="utf-8"))["pages"]) == 64


def test_lookup_rejects_private_dns_answers(monkeypatch):
    import pdftr.parsing.mineru_cloud as cloud

    monkeypatch.setattr(cloud.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("192.168.1.1", 443))])
    request = httpx2.Request("GET", "https://public-name.example/result.zip")
    with pytest.raises(ParseError, match="不安全"):
        asyncio.run(cloud._validate_presigned_request(request))


def test_slow_upload_starts_cloud_progress_at_zero(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = FakeMinerU()

    async def run():
        sent = asyncio.Event()
        progress = []
        pages = iter((1, 2, 1, 3))

        def report(message, fraction=None):
            progress.append((message, fraction))
            if message.startswith("正在上传") and fraction == 1.0:
                sent.set()

        async def handler(request):
            if request.method == "PUT":
                response = await fake(request)
                await asyncio.wait_for(sent.wait(), timeout=4)
                return response
            if "/extract-results/" in request.url.path:
                extracted = next(pages, None)
                item = {
                    "state": "running" if extracted is not None else "done",
                    "extract_progress": {"extracted_pages": extracted, "total_pages": 4},
                    "full_zip_url": ZIP,
                }
                return httpx2.Response(200, json={"data": {"extract_result": [item]}})
            return await fake(request)

        parser = MinerUCloudParser("tok", poll_interval=0, transport=httpx2.MockTransport(handler))
        await parser.parse(pdf, on_progress=report)
        assert sent.is_set()
        assert [value for msg, value in progress if msg.startswith("MinerU 正在解析")] == [0.25, 0.5, 0.75]
        assert progress[-2:] == [("正在下载解析结果", 0.0), ("下载解析结果完成", 1.0)]

    asyncio.run(run())


@pytest.mark.parametrize("proxy", [None, "socks5h://proxy.example:1080"])
def test_presigned_hooks_retry_transient_dns_directly_and_skip_dns_through_proxy(tmp_path, monkeypatch, proxy):
    import pdftr.parsing.mineru_cloud as cloud

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = FakeMinerU()
    resolved = []
    client = httpx2.AsyncClient

    def mocked_client(**kwargs):
        assert kwargs["proxy"] == proxy
        return client(
            headers=kwargs.get("headers"), transport=httpx2.MockTransport(fake), event_hooks=kwargs.get("event_hooks")
        )

    def resolve(host, port, **kwargs):
        assert proxy is None
        resolved.append(host)
        if resolved.count(host) == 1:
            raise OSError("暂时无法解析")
        return [(0, 0, 0, "", ("1.1.1.1", port))]

    monkeypatch.setattr(cloud.httpx2, "AsyncClient", mocked_client)
    monkeypatch.setattr(cloud.socket, "getaddrinfo", resolve)
    parser = MinerUCloudParser("tok", poll_interval=0, proxy=proxy, retry_delays=(0,))
    assert asyncio.run(parser.parse(pdf)).pages
    assert resolved == ([] if proxy is not None else ["oss.example", "oss.example", "cdn.example", "cdn.example"])
    assert sum(line.startswith("POST ") for line in fake.seen) == 1


@pytest.mark.parametrize("url", ["https://service.local/result.zip", "https://127.1/result.zip"])
def test_proxy_request_hook_still_rejects_unsafe_presigned_url(monkeypatch, url):
    import pdftr.parsing.mineru_cloud as cloud

    def forbidden(*args, **kwargs):
        pytest.fail("代理模式不应查询本机 DNS")

    monkeypatch.setattr(cloud.socket, "getaddrinfo", forbidden)
    request = httpx2.Request("GET", url)
    with pytest.raises(ParseError, match="不安全"):
        asyncio.run(cloud._validate_presigned_request(request, resolve_dns=False))


@pytest.mark.parametrize("stage", ["download", "normalize", "connection"])
def test_shard_timeout_limits_outer_retries(tmp_path, monkeypatch, stage):
    import time

    import pdftr.parsing.mineru_cloud as cloud

    pdf = _shard_job(tmp_path / "job")
    fake = FakeShardedMinerU()
    entered = []

    async def handler(request):
        if request.method == "GET" and request.url.host == "cdn.example" and stage != "normalize":
            entered.append(stage)
            if stage == "connection":
                raise httpx2.ReadTimeout("连接超时")
            await asyncio.Future()
        return await fake(request)

    if stage == "normalize":
        original_normalize = cloud._normalize_archive

        def normalize(*args, **kwargs):
            entered.append("normalize")
            time.sleep(0.2)
            return original_normalize(*args, **kwargs)

        monkeypatch.setattr(cloud, "_normalize_archive", normalize)

    parser = MinerUCloudParser(
        "tok",
        poll_interval=0,
        enable_sharding=True,
        use_pdf_parent_tmp=True,
        retry_delays=(),
        transport=httpx2.MockTransport(handler),
    )
    download = parser._download

    async def short_download(client, url, raw_dir, deadline_at, **kwargs):
        return await download(client, url, raw_dir, min(deadline_at, monotonic() + 0.12), **kwargs)

    if stage != "connection":
        monkeypatch.setattr(parser, "_download", short_download)
    message = "连接 MinerU 云端超时" if stage == "connection" else "超过本次解析总时限"
    with pytest.raises(ParseError, match=message) as error:
        asyncio.run(parser.parse(pdf))
    assert entered and error.value.retryable and error.value.max_retries == 1
    assert not (pdf.parent / "parser-tmp").exists()


def test_progress_aggregates_running_pages_and_finished_shards(tmp_path, caplog):
    import logging

    fake = FakeShardedMinerU(saturation={"GET": 2})
    pdf = _shard_job(tmp_path / "job")
    progress = []
    polls = 0

    async def handler(request):
        nonlocal polls
        response = await fake(request)
        if "/extract-results/" not in request.url.path:
            return response
        polls += 1
        if polls > 2:
            return response
        payload = response.json()
        items = payload["data"]["extract_result"]
        for i, item in enumerate(items):
            if polls == 1 or i != 0:
                item["state"] = "running"
                item["extract_progress"] = {"extracted_pages": 2 if polls == 1 else 19, "total_pages": 19}
        return httpx2.Response(200, json=payload)

    parser = MinerUCloudParser("tok", poll_interval=0, enable_sharding=True, transport=httpx2.MockTransport(handler))
    with caplog.at_level(logging.INFO):
        asyncio.run(parser.parse(pdf, on_progress=lambda message, fraction=None: progress.append((message, fraction))))
    parsed = [(msg, value) for msg, value in progress if msg.startswith("MinerU 正在解析")]
    assert len(parsed) == 2
    assert "第 8/52 页（15%），已完成 0/4 片" in parsed[0][0]
    assert "第 52/52 页（100%），已完成 1/4 片" in parsed[1][0]
    assert [value for _, value in parsed] == pytest.approx([8 / 52, 1.0])
    record = next(record for record in caplog.records if record.getMessage().startswith("MinerU 解析完成"))
    assert isinstance(record.args, tuple)
    phase_seconds = record.args[4:-1]
    assert all(seconds >= 0 for seconds in phase_seconds)
    assert phase_seconds[-1] > 0
    assert sum(phase_seconds) <= record.args[-1]


@pytest.mark.parametrize("reason", ["加密", "损坏"])
def test_permanent_shard_error_does_not_resubmit(tmp_path, reason):
    fake = FakeShardedMinerU(fail_first=True)
    pdf = _shard_job(tmp_path / "job")

    async def handler(request):
        response = await fake(request)
        if "/extract-results/" in request.url.path:
            payload = response.json()
            for item in payload["data"]["extract_result"]:
                if item["state"] == "failed":
                    item["err_msg"] = reason
            return httpx2.Response(200, json=payload)
        return response

    parser = MinerUCloudParser("tok", poll_interval=0, enable_sharding=True, transport=httpx2.MockTransport(handler))
    with pytest.raises(ParseError, match=reason) as error:
        asyncio.run(parser.parse(pdf))
    assert not error.value.retryable
    assert len(fake.batches) == 1 and not fake.downloaded
