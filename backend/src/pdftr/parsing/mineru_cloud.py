"""MinerU 精准解析 API 适配器（https://mineru.net）。

流程（S2 验证）：申请上传链接 → PUT 上传（预签名地址，不带鉴权头）→ 轮询批次结果 → 下载 Zip → 读 layout.json。
免费额度：每天 1000 页最高优先级，超出后排队变慢；单文件 ≤200 MB、≤200 页。
"""

import asyncio
import contextlib
import json
import logging
import tempfile
import time
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx2

from pdftr.domain.layout import LayoutResult
from pdftr.parsing.mineru_normalize import normalize
from pdftr.parsing.port import ParseError, ParseProgress

API = "https://mineru.net/api/v4"
UPLOAD_CHUNK = 64 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_LAYOUT_BYTES = 64 * 1024 * 1024

log = logging.getLogger(__name__)


async def _join_task[T](task: asyncio.Task[T]) -> None:
    """等子任务释放文件后再向上传递回收期间收到的取消。"""
    joined = asyncio.gather(task, return_exceptions=True)
    re_cancelled = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            re_cancelled = True
    if re_cancelled:
        raise asyncio.CancelledError


class MinerUCloudParser:
    name = "mineru-cloud"
    is_cloud = True

    def __init__(
        self,
        token: str,
        *,
        model_version: str = "vlm",
        language: str = "en",
        poll_interval: float = 5.0,
        timeout: float = 300.0,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        stall_timeout: float = 60.0,
        proxy: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ParseError("未配置 MinerU Token")
        self._token = token
        self._model_version = model_version
        self._language = language
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._stall_timeout = stall_timeout
        # 只有发往 MinerU 的请求走代理（服务器到国内线路差时用，见 PDFTR_MINERU_PROXY）；有注入的传输层时不用
        self._proxy = None if transport is not None else (proxy or None)
        self._transport = transport  # 测试时注入假传输层

    async def parse(
        self,
        pdf: Path,
        pages: list[int] | None = None,
        raw_dir: Path | None = None,
        on_progress: ParseProgress | None = None,
    ) -> LayoutResult:
        try:
            return await self._parse(pdf, pages, raw_dir, on_progress or _ignore)
        except httpx2.TimeoutException as e:
            raise ParseError("连接 MinerU 云端超时，请检查服务器能否访问 mineru.net", retryable=True) from e
        except httpx2.TransportError as e:
            raise ParseError(
                "无法连接 MinerU 云端，请检查服务器网络（Docker 部署时确认容器能访问外网）", retryable=True
            ) from e

    async def _parse(
        self, pdf: Path, pages: list[int] | None, raw_dir: Path | None, on_progress: ParseProgress
    ) -> LayoutResult:
        deadline_at = time.monotonic() + self._timeout
        headers = {"Authorization": f"Bearer {self._token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(120, connect=15),
                               transport=self._transport, proxy=self._proxy) as api,
            httpx2.AsyncClient(timeout=httpx2.Timeout(300, connect=15), transport=self._transport,
                               proxy=self._proxy, follow_redirects=True) as plain,
        ):  # fmt: skip
            on_progress("正在上传到 MinerU 云端")
            batch_id, upload_url = await self._request_upload(api, pdf, pages, deadline_at=deadline_at)
            size = pdf.stat().st_size
            started = time.monotonic()
            r = await self._upload(plain, upload_url, pdf, on_progress, deadline_at=deadline_at)
            _check(r, "上传文件")
            # 走代理时上传往往一两秒就结束，进度来不及看到：把上传结果带进后续的状态里一直显示
            uploaded = upload_summary(size, time.monotonic() - started)

            def with_upload(message: str, fraction: float | None = None) -> None:
                on_progress(f"{message}（{uploaded}）", fraction)

            zip_url = await self._wait(api, batch_id, with_upload, deadline_at=deadline_at)
            on_progress("正在下载解析结果", 1.0)
            return await self._download(plain, zip_url, raw_dir, deadline_at)

    async def _download(
        self, client: httpx2.AsyncClient, url: str, raw_dir: Path | None, deadline_at: float
    ) -> LayoutResult:
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=raw_dir, prefix="mineru-", suffix=".part", delete=False) as f:
                tmp = Path(f.name)
                async with asyncio.timeout(self._remaining(deadline_at)):
                    for delay in (*self._retry_delays, None):
                        try:
                            async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                                _check(response, "下载解析结果", read_json=False)
                                content_length = response.headers.get("Content-Length", "")
                                if (
                                    content_length.isascii()
                                    and content_length.isdecimal()
                                    and int(content_length) > MAX_ARCHIVE_BYTES
                                ):
                                    raise ParseError("MinerU 解析结果 ZIP 超过大小上限")
                                size = 0
                                async for chunk in response.aiter_bytes(UPLOAD_CHUNK):
                                    size += len(chunk)
                                    if size > MAX_ARCHIVE_BYTES:
                                        raise ParseError("MinerU 解析结果 ZIP 超过大小上限")
                                    f.write(chunk)
                            break
                        except httpx2.TransportError as e:
                            if delay is None:
                                raise
                            f.seek(0)
                            f.truncate()
                            log.warning("下载 MinerU 结果出错（%s），%.0f 秒后重试", type(e).__name__, delay)
                            await asyncio.sleep(delay)
            worker = asyncio.create_task(asyncio.to_thread(_normalize_archive, tmp))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await _join_task(worker)
                raise
            if raw_dir is not None:
                tmp.replace(raw_dir / "mineru.zip")
            return result
        except TimeoutError as e:
            raise httpx2.TimeoutException("下载 MinerU 结果超过剩余解析时限") from e
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def _request_upload(
        self, api: httpx2.AsyncClient, pdf: Path, pages: list[int] | None, *, deadline_at: float | None = None
    ) -> tuple[str, str]:
        # 始终整份解析：MinerU 按 page_ranges 解析时返回的页码是否相对子集未经验证，
        # 用户选择的页码范围在翻译阶段筛选（pages 参数在这里不使用）。
        del pages
        file = {"name": pdf.name, "data_id": pdf.stem[:128]}
        r = await self._send(
            api,
            "POST",
            f"{API}/file-urls/batch",
            deadline=self._remaining(deadline_at),
            json={
                "files": [file],
                "model_version": self._model_version,
                "enable_formula": True,
                "enable_table": True,
                "language": self._language,
            },
        )
        body = _check(r, "申请上传链接")
        try:
            return body["data"]["batch_id"], body["data"]["file_urls"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise ParseError(f"MinerU 返回格式异常：{body}") from e

    async def _wait(
        self,
        api: httpx2.AsyncClient,
        batch_id: str,
        on_progress: ParseProgress,
        *,
        deadline_at: float | None = None,
    ) -> str:
        deadline = deadline_at or time.monotonic() + self._timeout
        last = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r = await self._send(
                api,
                "GET",
                f"{API}/extract-results/batch/{batch_id}",
                deadline=min(180.0, remaining),
            )
            body = _check(r, "查询解析进度")
            result = (body.get("data") or {}).get("extract_result") or [{}]
            state = result[0].get("state")
            if state == "done":
                return result[0]["full_zip_url"]
            if state == "failed":
                raise ParseError(f"MinerU 解析失败：{result[0].get('err_msg') or '未知原因'}")
            status = _describe(state, result[0].get("extract_progress") or {})
            if status != last:
                on_progress(*status)
                last = status
            await asyncio.sleep(self._poll_interval)
        on_progress("MinerU 云端解析等待超过 5 分钟，准备重新提交", None)
        raise ParseError("MinerU 解析超过 5 分钟，正在自动重试", retryable=True)

    async def _send(
        self, client: httpx2.AsyncClient, method: str, url: str, *, deadline: float = 180, **kwargs: Any
    ) -> httpx2.Response:
        """网络偶发中断（连接超时、连接被重置）时按退避间隔重试；HTTP 错误码不重试。

        deadline 是这次请求（包括内部重试）的总时限，不能因为重试而重复累加。
        """
        deadline_at = time.monotonic() + deadline
        for delay in (*self._retry_delays, None):
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise httpx2.TimeoutException(f"{method} 超过 {deadline:.0f} 秒未完成")
            try:
                return await asyncio.wait_for(client.request(method, url, **kwargs), remaining)
            except (httpx2.TransportError, TimeoutError) as e:
                if isinstance(e, TimeoutError):
                    raise httpx2.TimeoutException(f"{method} 超过 {deadline:.0f} 秒未完成") from e
                if delay is None:
                    raise
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise httpx2.TimeoutException(f"{method} 超过 {deadline:.0f} 秒未完成") from e
                log.warning("访问 MinerU 网络出错（%s），%.0f 秒后重试", type(e).__name__, delay)
                await asyncio.sleep(min(delay, remaining))
        raise AssertionError("unreachable")

    async def _upload(
        self,
        client: httpx2.AsyncClient,
        url: str,
        pdf: Path,
        on_progress: ParseProgress,
        *,
        deadline_at: float | None = None,
    ) -> httpx2.Response:
        """分块上传并每秒汇报进度（已传多少、速度、预计剩余时间）。

        只在连续 stall_timeout 秒没有任何进展时才中断重传：网络慢但仍在推进时不打断
        （此前按文件大小设总时限，慢速链路上会把快传完的上传掐断重来）。预签名地址不支持续传，重传从头开始。
        """
        size = pdf.stat().st_size
        attempts = len(self._retry_delays) + 1
        for n, delay in enumerate((*self._retry_delays, None), start=1):
            remaining = self._remaining(deadline_at)
            if remaining <= 0:
                raise ParseError("上传到 MinerU 云端超过本次解析总时限，请稍后重试", retryable=True)
            sent, start = 0, time.monotonic()
            last = start

            headers = {"Content-Length": str(size)}  # 预签名上传需要精确长度
            try:
                with pdf.open("rb") as source:

                    async def body() -> AsyncGenerator[bytes]:
                        nonlocal sent, last
                        while chunk := source.read(UPLOAD_CHUNK):
                            yield chunk
                            sent, last = sent + len(chunk), time.monotonic()

                    async with contextlib.aclosing(body()) as stream:
                        task = asyncio.create_task(client.request("PUT", url, content=stream, headers=headers))
                        try:
                            while True:
                                done, _ = await asyncio.wait({task}, timeout=min(1.0, remaining, self._stall_timeout))
                                if done:
                                    return task.result()
                                now = time.monotonic()
                                if deadline_at is not None and now >= deadline_at:
                                    raise ParseError("上传到 MinerU 云端超过本次解析总时限，请稍后重试", retryable=True)
                                if now - last >= self._stall_timeout:
                                    raise TimeoutError
                                retry = f"（第 {n}/{attempts} 次尝试）" if n > 1 else ""
                                on_progress(upload_status(sent, size, now - start) + retry, sent / max(1, size))
                                remaining = self._remaining(deadline_at)
                        finally:
                            if not task.done():
                                task.cancel()
                            # 回收内部请求后才能关闭文件或开始重传。
                            await _join_task(task)
            except (httpx2.TransportError, TimeoutError) as e:
                if delay is None:
                    reason = (
                        f"网络连续 {self._stall_timeout:g} 秒没有进展"
                        if isinstance(e, TimeoutError)
                        else "网络连接中断"
                    )
                    raise ParseError(
                        f"上传到 MinerU 云端失败：{reason}，请稍后重试；"
                        "服务器到国内线路较差时可配置 PDFTR_MINERU_PROXY",
                        retryable=True,
                    ) from e
                log.warning("上传到 MinerU 卡住或出错（%s），%.0f 秒后重传", type(e).__name__, delay)
                await asyncio.sleep(min(delay, self._remaining(deadline_at)))
        raise AssertionError("unreachable")

    def _remaining(self, deadline_at: float | None) -> float:
        """返回本次解析总时限内还能使用的秒数。"""
        if deadline_at is None:
            return 180.0
        return max(0.0, deadline_at - time.monotonic())


def upload_status(sent: int, total: int, elapsed: float, service: str = "MinerU") -> str:
    """“正在上传到 MinerU 云端：1.2 / 3.4 MB，32 KB/s，约 1 分 10 秒”。"""
    mb = lambda n: f"{n / 1048576:.1f}"  # noqa: E731
    text = f"正在上传到 {service} 云端：{mb(sent)} / {mb(total)} MB"
    if elapsed < 1 or sent == 0:
        return text
    speed = sent / elapsed
    rate = f"{speed / 1048576:.1f} MB/s" if speed >= 1048576 else f"{speed / 1024:.0f} KB/s"
    left = (total - sent) / speed
    eta = f"约 {left:.0f} 秒" if left < 60 else f"约 {left // 60:.0f} 分 {left % 60:.0f} 秒"
    return f"{text}，{rate}，{eta}" if sent < total else f"{text}，{rate}，等待对方确认"


def upload_summary(size: int, elapsed: float) -> str:
    """“已上传 2.1 MB，用时 1.2 秒，1.8 MB/s”。"""
    speed = size / max(elapsed, 0.001)
    rate = f"{speed / 1048576:.1f} MB/s" if speed >= 1048576 else f"{speed / 1024:.0f} KB/s"
    return f"已上传 {size / 1048576:.1f} MB，用时 {elapsed:.1f} 秒，{rate}"


def _ignore(message: str, fraction: float | None = None) -> None:
    pass


def _describe(state: str | None, progress: dict) -> tuple[str, float | None]:
    """MinerU 批次状态 → 面向用户的说明与完成比例。"""
    if state == "running":
        done, total = progress.get("extracted_pages"), progress.get("total_pages")
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            percent = done / total * 100
            return f"MinerU 正在解析，第 {done}/{total} 页（{percent:.0f}%）", done / total
        return "MinerU 正在解析，正在等待页数进度", None
    if state == "converting":
        return "MinerU 正在整理解析结果（解析已完成）", 1.0
    if state == "waiting-file":
        return "MinerU 正在接收上传文件", None
    if state == "pending":
        return "MinerU 已接收任务，正在等待解析资源（队列中）", None
    if state:
        return f"MinerU 返回状态：{state}，正在等待更新", None
    return "MinerU 已提交任务，正在等待状态更新", None


def _check(r: httpx2.Response, action: str, *, read_json: bool = True) -> dict:
    if r.status_code in (401, 403):
        raise ParseError("MinerU Token 无效或已过期")
    if r.status_code == 429:
        raise ParseError("MinerU 请求过于频繁或额度不足，请稍后再试")
    if r.status_code >= 400:
        raise ParseError(f"MinerU {action}失败：HTTP {r.status_code}", retryable=r.status_code >= 500)
    if not read_json or not r.headers.get("content-type", "").startswith("application/json"):
        return {}
    body = r.json()
    if isinstance(body, dict) and body.get("code", 0) != 0:
        raise ParseError(f"MinerU {action}失败：{body.get('msg') or body}")
    return body


def _normalize_archive(archive: Path) -> LayoutResult:
    return normalize(_read_layout(archive))


def _read_layout(archive: Path) -> dict:
    try:
        with zipfile.ZipFile(archive) as z:
            info = next((item for item in z.infolist() if item.filename.endswith("layout.json")), None)
            if info is None:
                raise ParseError("MinerU 结果中缺少 layout.json")
            if info.file_size > MAX_LAYOUT_BYTES:
                raise ParseError("MinerU 结果的 layout.json 解压后超过大小上限")
            with z.open(info) as layout:
                data = layout.read(MAX_LAYOUT_BYTES + 1)
            if len(data) > MAX_LAYOUT_BYTES:
                raise ParseError("MinerU 结果的 layout.json 解压后超过大小上限")
            parsed = json.loads(data)
            if not isinstance(parsed, dict):
                raise ParseError("MinerU 结果的 layout.json 格式异常")
            return parsed
    except ParseError:
        raise
    except (zipfile.BadZipFile, ValueError, RuntimeError) as e:
        raise ParseError("MinerU 结果 ZIP 或 layout.json 格式异常") from e
