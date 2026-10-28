"""MinerU 精准解析 API 适配器（https://mineru.net）。

流程（S2 验证）：申请上传链接 → PUT 上传（预签名地址，不带鉴权头）→ 轮询批次结果 → 下载 Zip → 读 layout.json。
免费额度：每天 1000 页最高优先级，超出后排队变慢；单文件 ≤200 MB、≤200 页。
"""

import asyncio
import io
import json
import logging
import time
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2

from pdftr.domain.layout import LayoutResult
from pdftr.parsing.mineru_normalize import normalize
from pdftr.parsing.port import ParseError, ParseProgress

API = "https://mineru.net/api/v4"
UPLOAD_CHUNK = 64 * 1024

log = logging.getLogger(__name__)


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
        timeout: float = 900.0,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        stall_timeout: float = 60.0,
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
            raise ParseError("连接 MinerU 云端超时，请检查服务器能否访问 mineru.net") from e
        except httpx2.TransportError as e:
            raise ParseError("无法连接 MinerU 云端，请检查服务器网络（Docker 部署时确认容器能访问外网）") from e

    async def _parse(
        self, pdf: Path, pages: list[int] | None, raw_dir: Path | None, on_progress: ParseProgress
    ) -> LayoutResult:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(120, connect=15),
                               transport=self._transport) as api,
            httpx2.AsyncClient(timeout=httpx2.Timeout(300, connect=15), transport=self._transport,
                               follow_redirects=True) as plain,
        ):  # fmt: skip
            on_progress("正在上传到 MinerU 云端")
            batch_id, upload_url = await self._request_upload(api, pdf, pages)
            r = await self._upload(plain, upload_url, pdf.read_bytes(), on_progress)
            _check(r, "上传文件")
            zip_url = await self._wait(api, batch_id, on_progress)
            on_progress("正在下载解析结果", 1.0)
            r = await self._send(plain, "GET", zip_url, deadline=300)
            _check(r, "下载解析结果")
        data = r.content
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "mineru.zip").write_bytes(data)
        return normalize(_read_layout(data))

    async def _request_upload(self, api: httpx2.AsyncClient, pdf: Path, pages: list[int] | None) -> tuple[str, str]:
        # 始终整份解析：MinerU 按 page_ranges 解析时返回的页码是否相对子集未经验证，
        # 用户选择的页码范围在翻译阶段筛选（pages 参数在这里不使用）。
        del pages
        file = {"name": pdf.name, "data_id": pdf.stem[:128]}
        r = await self._send(
            api,
            "POST",
            f"{API}/file-urls/batch",
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

    async def _wait(self, api: httpx2.AsyncClient, batch_id: str, on_progress: ParseProgress) -> str:
        deadline = time.monotonic() + self._timeout
        last = None
        while time.monotonic() < deadline:
            r = await self._send(api, "GET", f"{API}/extract-results/batch/{batch_id}")
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
        raise ParseError("MinerU 解析超时")

    async def _send(
        self, client: httpx2.AsyncClient, method: str, url: str, *, deadline: float = 180, **kwargs: Any
    ) -> httpx2.Response:
        """网络偶发中断（连接超时、连接被重置）时按退避间隔重试；HTTP 错误码不重试。

        deadline 是单次请求的总时限：httpx 的超时只限制“单次读写”，网络时断时续时每次都能挤出一点数据，
        永远不会超时（实测上传 4 MB 的 PDF 卡了 7 分钟以上），所以另设总时限。
        """
        for delay in (*self._retry_delays, None):
            try:
                return await asyncio.wait_for(client.request(method, url, **kwargs), deadline)
            except (httpx2.TransportError, TimeoutError) as e:
                if delay is None:
                    if isinstance(e, TimeoutError):
                        raise httpx2.TimeoutException(f"{method} 超过 {deadline:.0f} 秒未完成") from e
                    raise
                log.warning("访问 MinerU 网络出错（%s），%.0f 秒后重试", type(e).__name__, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _upload(
        self, client: httpx2.AsyncClient, url: str, data: bytes, on_progress: ParseProgress
    ) -> httpx2.Response:
        """分块上传并每秒汇报进度（已传多少、速度、预计剩余时间）。

        只在连续 stall_timeout 秒没有任何进展时才中断重传：网络慢但仍在推进时不打断
        （此前按文件大小设总时限，慢速链路上会把快传完的上传掐断重来）。预签名地址不支持续传，重传从头开始。
        """
        attempts = len(self._retry_delays) + 1
        for n, delay in enumerate((*self._retry_delays, None), start=1):
            sent, start = 0, time.monotonic()
            last = start

            async def body() -> AsyncIterator[bytes]:
                nonlocal sent, last
                for i in range(0, len(data), UPLOAD_CHUNK):
                    yield data[i : i + UPLOAD_CHUNK]
                    sent, last = min(i + UPLOAD_CHUNK, len(data)), time.monotonic()

            headers = {"Content-Length": str(len(data))}  # 显式给出长度：预签名上传不接受分块传输编码
            task = asyncio.ensure_future(client.request("PUT", url, content=body(), headers=headers))
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=1.0)
                    if done:
                        return task.result()
                    now = time.monotonic()
                    if now - last > self._stall_timeout:
                        raise TimeoutError
                    retry = f"（第 {n}/{attempts} 次尝试）" if n > 1 else ""
                    on_progress(upload_status(sent, len(data), now - start) + retry, sent / len(data))
            except (httpx2.TransportError, TimeoutError) as e:
                task.cancel()
                if delay is None:
                    raise ParseError(
                        f"上传到 MinerU 云端失败：网络连续 {self._stall_timeout:.0f} 秒没有进展，"
                        "请稍后重试；服务器到国内线路较差时可配置 PDFTR_MINERU_PROXY"
                    ) from e
                log.warning("上传到 MinerU 卡住或出错（%s），%.0f 秒后重传", type(e).__name__, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")


def upload_status(sent: int, total: int, elapsed: float) -> str:
    """“正在上传到 MinerU 云端：1.2 / 3.4 MB，32 KB/s，约 1 分 10 秒”。"""
    mb = lambda n: f"{n / 1048576:.1f}"  # noqa: E731
    text = f"正在上传到 MinerU 云端：{mb(sent)} / {mb(total)} MB"
    if elapsed < 1 or sent == 0:
        return text
    speed = sent / elapsed
    rate = f"{speed / 1048576:.1f} MB/s" if speed >= 1048576 else f"{speed / 1024:.0f} KB/s"
    left = (total - sent) / speed
    eta = f"约 {left:.0f} 秒" if left < 60 else f"约 {left // 60:.0f} 分 {left % 60:.0f} 秒"
    return f"{text}，{rate}，{eta}" if sent < total else f"{text}，{rate}，等待对方确认"


def _ignore(message: str, fraction: float | None = None) -> None:
    pass


def _describe(state: str | None, progress: dict) -> tuple[str, float | None]:
    """MinerU 批次状态 → 面向用户的说明与完成比例。"""
    if state == "running":
        done, total = progress.get("extracted_pages"), progress.get("total_pages")
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            return f"MinerU 正在解析，第 {done}/{total} 页", done / total
        return "MinerU 正在解析", None
    if state == "converting":
        return "MinerU 正在整理解析结果", 1.0
    if state == "waiting-file":
        return "等待 MinerU 接收文件", None
    return "MinerU 排队中（免费额度高峰时可能要等 10 分钟以上）", None


def _check(r: httpx2.Response, action: str) -> dict:
    if r.status_code in (401, 403):
        raise ParseError("MinerU Token 无效或已过期")
    if r.status_code == 429:
        raise ParseError("MinerU 请求过于频繁或额度不足，请稍后再试")
    if r.status_code >= 400:
        raise ParseError(f"MinerU {action}失败：HTTP {r.status_code}")
    if not r.headers.get("content-type", "").startswith("application/json"):
        return {}
    body = r.json()
    if isinstance(body, dict) and body.get("code", 0) != 0:
        raise ParseError(f"MinerU {action}失败：{body.get('msg') or body}")
    return body


def _read_layout(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = next((n for n in z.namelist() if n.endswith("layout.json")), None)
        if name is None:
            raise ParseError("MinerU 结果中缺少 layout.json")
        return json.loads(z.read(name))
