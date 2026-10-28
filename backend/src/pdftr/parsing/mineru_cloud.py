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
from pathlib import Path
from typing import Any

import httpx2

from pdftr.domain.layout import LayoutResult
from pdftr.parsing.mineru_normalize import normalize
from pdftr.parsing.port import ParseError, ParseProgress

API = "https://mineru.net/api/v4"

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
            r = await self._send(plain, "PUT", upload_url, content=pdf.read_bytes())
            _check(r, "上传文件")
            zip_url = await self._wait(api, batch_id, on_progress)
            on_progress("正在下载解析结果", 1.0)
            r = await self._send(plain, "GET", zip_url)
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

    async def _send(self, client: httpx2.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        """网络偶发中断（连接超时、连接被重置）时按退避间隔重试；HTTP 错误码不重试。"""
        for delay in (*self._retry_delays, None):
            try:
                return await client.request(method, url, **kwargs)
            except httpx2.TransportError:
                if delay is None:
                    raise
                log.warning("访问 MinerU 网络出错，%.0f 秒后重试", delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")


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
