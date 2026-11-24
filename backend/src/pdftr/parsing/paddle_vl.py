"""百度飞桨 PaddleOCR-VL 云端解析（星河社区 API，https://aistudio.baidu.com/paddleocr）。

流程：上传 PDF 提交任务 → 轮询 → 下载 JSONL 结果。结果只有块坐标（像素）与块内容，行内公式写成 $LaTeX$、
没有位置；这里只换算坐标、对照块类型，行与行内公式位置由融合阶段对照 PDF 字符推算（fusion/formula_align.py）。

与 MinerU 的对比见 docs/02-spikes.md：文字准确率相当、公式 LaTeX 更干净、解析更快、海外服务器不走代理也能用；
但没有行级与行内公式坐标，默认解析服务仍是 MinerU。
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx2
import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutPage, LayoutResult
from pdftr.domain.url_guard import build_safe_async_transport, validate_outbound_request_url
from pdftr.parsing.mineru_cloud import upload_status
from pdftr.parsing.port import ParseError, ParseProgress

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"
CHUNK = 64 * 1024
MAX_RESULT_BYTES = 100 * 1024 * 1024

log = logging.getLogger(__name__)

# 块类型对照；不在表里的按正文处理
_TYPES: dict[str, tuple[BlockType, bool]] = {  # 类型, 是否丢弃（不展示、不翻译）
    "doc_title": (BlockType.TITLE, False),
    "paragraph_title": (BlockType.TITLE, False),
    "text": (BlockType.TEXT, False),
    "abstract": (BlockType.TEXT, False),
    "content": (BlockType.TEXT, False),
    "algorithm": (BlockType.CODE, False),
    "reference_content": (BlockType.REFERENCE, False),
    "reference": (BlockType.REFERENCE, False),
    "figure_title": (BlockType.CAPTION, False),
    "table_title": (BlockType.CAPTION, False),
    "chart_title": (BlockType.CAPTION, False),
    "footnote": (BlockType.FOOTNOTE, False),
    "display_formula": (BlockType.FORMULA, False),
    "formula": (BlockType.FORMULA, False),
    "formula_number": (BlockType.FORMULA, False),
    "table": (BlockType.TABLE, False),
    "chart": (BlockType.FIGURE, False),
    "image": (BlockType.FIGURE, False),
    "figure": (BlockType.FIGURE, False),
    "header_image": (BlockType.FIGURE, True),
    "footer_image": (BlockType.FIGURE, True),
    "header": (BlockType.HEADER, True),
    "footer": (BlockType.FOOTER, True),
    "number": (BlockType.FOOTER, True),
    "aside_text": (BlockType.HEADER, True),
}


def normalize(jsonl: str, page_sizes: list[tuple[float, float]]) -> LayoutResult:
    """JSONL 结果 → 统一的版面结构。page_sizes 为 PDF 各页尺寸（点），用于把像素坐标换算成点。"""
    pages_raw = [pg for line in jsonl.splitlines() if line.strip()
                 for pg in json.loads(line)["result"]["layoutParsingResults"]]  # fmt: skip
    pages: list[LayoutPage] = []
    for index, (raw, (width, height)) in enumerate(zip(pages_raw, page_sizes, strict=False)):
        pr = raw["prunedResult"]
        k = width / pr["width"]
        blocks: list[LayoutBlock] = []
        for order, b in enumerate(pr.get("parsing_res_list", [])):
            label = b.get("block_label", "")
            btype, discarded = _TYPES.get(label, (BlockType.TEXT, False))
            x0, y0, x1, y1 = (v * k for v in b["block_bbox"])
            content = (b.get("block_content") or "").strip()
            latex = re.sub(r"^\$+|\$+$", "", content).strip() if btype is BlockType.FORMULA and content else None
            blocks.append(
                LayoutBlock(
                    type=btype,
                    bbox=Rect(x0=x0, y0=y0, x1=x1, y1=y1),
                    order=order,
                    latex=latex,
                    level=1 if label == "doc_title" else None,
                    discarded=discarded,
                    source_type=label,
                    # 公式、表格、算法框不翻译，不需要拆行与公式定位
                    content=None if btype in (BlockType.FORMULA, BlockType.TABLE, BlockType.CODE) else content,
                )
            )
        pages.append(LayoutPage(index=index, width=width, height=height, blocks=blocks))
    return LayoutResult(parser="paddleocr-vl", parser_version=MODEL, pages=pages)


class PaddleVLParser:
    name = "paddleocr-vl"
    is_cloud = True

    def __init__(
        self,
        token: str,
        *,
        poll_interval: float = 3.0,
        timeout: float = 900.0,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        stall_timeout: float = 60.0,
        proxy: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        allow_private: bool = False,
    ) -> None:
        if not token:
            raise ParseError("未配置 PaddleOCR Token")
        self._token = token
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._stall_timeout = stall_timeout
        self._proxy = None if transport is not None else (proxy or None)
        self._transport = transport
        self._allow_private = allow_private

    def _build_transport(self) -> httpx2.AsyncBaseTransport:
        if self._transport is not None:
            return self._transport
        return build_safe_async_transport(proxy=self._proxy, allow_private=self._allow_private)

    async def _guard_outbound_request(self, request: httpx2.Request, *, label: str) -> None:
        if self._allow_private:
            return
        try:
            validate_outbound_request_url(
                str(request.url),
                allow_private=False,
                require_https=True,
                label=label,
                resolve_dns=self._transport is None,
            )
        except ValueError as exc:
            raise ParseError(f"PaddleOCR {label}不安全：{exc}", retryable=False) from None

    async def _guard_api_request(self, request: httpx2.Request) -> None:
        await self._guard_outbound_request(request, label="服务地址")

    async def _guard_plain_request(self, request: httpx2.Request) -> None:
        await self._guard_outbound_request(request, label="文件地址")

    async def check_connection(self) -> tuple[int, int]:
        """轻量探测 PaddleOCR-VL 连通性与 Token 有效性，返回 (http_status, latency_ms)。"""
        headers = {"Authorization": f"bearer {self._token}"}
        started = time.monotonic()
        hooks = {"request": [self._guard_api_request]} if not self._allow_private else None
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(10.0, connect=10.0),
                transport=self._build_transport(),
                follow_redirects=False,
                event_hooks=hooks,
            ) as client:
                r = await client.get(f"{JOB_URL}/probe")
        except httpx2.TimeoutException as e:
            raise ParseError("连接 PaddleOCR 云端超时，请检查服务器能否访问 aistudio-app.com", retryable=True) from e
        except httpx2.TransportError as e:
            raise ParseError("无法连接 PaddleOCR 云端，请检查服务器网络", retryable=True) from e
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if r.status_code in (401, 403):
            raise ParseError("PaddleOCR Token 无效或权限不足")
        if 300 <= r.status_code < 400:
            raise ParseError(f"PaddleOCR 返回异常重定向（HTTP {r.status_code}）")
        if r.status_code == 429:
            raise ParseError("PaddleOCR 请求过于频繁或额度不足，请稍后再试")
        if r.status_code >= 500:
            raise ParseError(f"PaddleOCR 服务暂时不可用（HTTP {r.status_code}）", retryable=True)
        if r.status_code not in (200, 404):
            raise ParseError(f"PaddleOCR 连接测试失败（HTTP {r.status_code}）")
        content_type = r.headers.get("content-type", "").lower()
        if "json" not in content_type:
            raise ParseError("PaddleOCR 返回格式异常，请检查服务地址与网络")
        try:
            body = r.json()
        except ValueError as e:
            raise ParseError("PaddleOCR 返回格式异常") from e
        if not isinstance(body, dict) or not body:
            raise ParseError("PaddleOCR 返回格式异常")
        raw_code = body.get("code")
        raw_err_code = body.get("errorCode")
        code_str = "" if raw_code is None or isinstance(raw_code, bool) else str(raw_code).strip()
        err_code_str = "" if raw_err_code is None or isinstance(raw_err_code, bool) else str(raw_err_code).strip()
        msg = str(body.get("msg") or body.get("errorMsg") or "")
        auth_codes = {"401", "403", "-10001", "-10002"}
        auth_keywords = ("token", "unauthorized", "auth", "过期", "无效", "权限")
        if code_str in auth_codes or err_code_str in auth_codes:
            raise ParseError("PaddleOCR Token 无效或权限不足")
        if r.status_code == 200:
            is_ok = code_str in ("0", "200") or err_code_str == "0"
            if not is_ok:
                if any(keyword in msg.lower() for keyword in auth_keywords):
                    raise ParseError("PaddleOCR Token 无效或权限不足")
                raise ParseError("PaddleOCR 服务返回异常业务状态，请检查 Token")
            if any(keyword in msg.lower() for keyword in auth_keywords):
                raise ParseError("PaddleOCR Token 无效或权限不足")
            return r.status_code, latency_ms
        # HTTP 404：仅当 JSON 对象包含明确的业务 404 码（且非鉴权错误）时才确认已通过鉴权
        if any(keyword in msg.lower() for keyword in auth_keywords):
            raise ParseError("PaddleOCR Token 无效或权限不足")
        if code_str != "404" and err_code_str != "404":
            raise ParseError("PaddleOCR 服务返回异常业务状态，请检查 Token")
        return r.status_code, latency_ms

    async def parse(
        self,
        pdf: Path,
        pages: list[int] | None = None,
        raw_dir: Path | None = None,
        on_progress: ParseProgress | None = None,
    ) -> LayoutResult:
        del pages  # 整份解析，页码范围在翻译阶段筛选（与 MinerU 一致）
        progress = on_progress or (lambda message, fraction=None: None)
        with pymupdf.open(pdf) as doc:
            sizes = [(p.rect.width, p.rect.height) for p in doc]
        try:
            jsonl = await self._run(pdf, progress)
        except httpx2.TimeoutException as e:
            raise ParseError("连接 PaddleOCR 云端超时，请检查服务器能否访问 aistudio-app.com", retryable=True) from e
        except httpx2.TransportError as e:
            raise ParseError("无法连接 PaddleOCR 云端，请检查服务器网络", retryable=True) from e
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "paddleocr.jsonl").write_text(jsonl, encoding="utf-8")
        return normalize(jsonl, sizes)

    async def _run(self, pdf: Path, progress: ParseProgress) -> str:
        headers = {"Authorization": f"bearer {self._token}"}
        timeout = httpx2.Timeout(120, connect=15)
        api_hooks = {"request": [self._guard_api_request]} if not self._allow_private else None
        plain_hooks = {"request": [self._guard_plain_request]} if not self._allow_private else None
        async with (
            httpx2.AsyncClient(
                headers=headers,
                timeout=timeout,
                transport=self._build_transport(),
                follow_redirects=False,
                event_hooks=api_hooks,
            ) as api,
            # 结果地址是预签名的，不能带鉴权头，强制禁止重定向并仅允许公网 HTTPS
            httpx2.AsyncClient(
                timeout=timeout,
                transport=self._build_transport(),
                follow_redirects=False,
                event_hooks=plain_hooks,
            ) as plain,
        ):
            progress("正在上传到 PaddleOCR 云端")
            job = await self._submit(api, pdf, progress)
            url = await self._wait(api, job, progress)
            progress("正在下载解析结果", 1.0)
            return await self._download_jsonl(plain, url)

    async def _download_jsonl(self, client: httpx2.AsyncClient, url: str) -> str:
        """有界流式下载 JSONL 解析结果，超过上限立即中断，防止超大响应撑爆内存。"""
        async with client.stream("GET", url) as r:
            _check(r, "下载解析结果", parse=False)
            content_length = r.headers.get("Content-Length", "")
            if content_length.isascii() and content_length.isdecimal() and int(content_length) > MAX_RESULT_BYTES:
                raise ParseError("PaddleOCR 返回结果过大")
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes(CHUNK):
                size += len(chunk)
                if size > MAX_RESULT_BYTES:
                    raise ParseError("PaddleOCR 返回结果过大")
                chunks.append(chunk)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError("PaddleOCR 返回结果编码异常") from exc

    async def _submit(self, client: httpx2.AsyncClient, pdf: Path, progress: ParseProgress) -> str:
        """流式分块上传并提交任务，每秒汇报进度；连续 stall_timeout 秒没有进展才重传（与 MinerU 一致）。"""
        options = {"useDocOrientationClassify": False, "useDocUnwarping": False, "useChartRecognition": False}
        boundary = "pdftr" + str(int(time.time() * 1000))
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{MODEL}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="optionalPayload"\r\n\r\n{json.dumps(options)}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{_safe(pdf.name)}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        file_size = pdf.stat().st_size
        total_length = len(head) + file_size + len(tail)
        attempts = len(self._retry_delays) + 1
        for n, delay in enumerate((*self._retry_delays, None), start=1):
            sent, start = 0, time.monotonic()
            last = start
            headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(total_length),
            }
            try:
                with pdf.open("rb") as source:

                    async def body() -> AsyncGenerator[bytes]:
                        nonlocal sent, last
                        yield head
                        last = time.monotonic()
                        while chunk := source.read(CHUNK):
                            yield chunk
                            sent += len(chunk)
                            last = time.monotonic()
                        yield tail
                        last = time.monotonic()

                    async with contextlib.aclosing(body()) as stream:
                        task = asyncio.create_task(client.post(JOB_URL, content=stream, headers=headers))
                        try:
                            while True:
                                done, _ = await asyncio.wait({task}, timeout=1.0)
                                if done:
                                    r = task.result()
                                    parsed = _check(r, "提交任务")
                                    try:
                                        return str(parsed["data"]["jobId"])
                                    except (KeyError, TypeError) as exc:
                                        raise ParseError("PaddleOCR 提交任务返回格式异常") from exc
                                now = time.monotonic()
                                if now - last > self._stall_timeout:
                                    raise TimeoutError
                                retry = f"（第 {n}/{attempts} 次尝试）" if n > 1 else ""
                                done_bytes = min(sent, file_size)
                                progress(
                                    upload_status(done_bytes, file_size, now - start, "PaddleOCR") + retry,
                                    done_bytes / max(1, file_size),
                                )
                        finally:
                            if not task.done():
                                task.cancel()
                            await _join_task(task)
            except (httpx2.TransportError, TimeoutError) as e:
                if delay is None:
                    raise ParseError(
                        f"上传到 PaddleOCR 云端失败：网络连续 {self._stall_timeout:.0f} 秒没有进展，请稍后重试",
                        retryable=True,
                    ) from e
                log.warning("上传到 PaddleOCR 卡住或出错（%s），%.0f 秒后重传", type(e).__name__, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _wait(self, client: httpx2.AsyncClient, job: str, progress: ParseProgress) -> str:
        deadline = time.monotonic() + self._timeout
        last = None
        while time.monotonic() < deadline:
            body = _check(await client.get(f"{JOB_URL}/{job}"), "查询解析进度")
            d = body.get("data") if isinstance(body, dict) else None
            if not isinstance(d, dict):
                raise ParseError("PaddleOCR 查询解析进度返回格式异常")
            state, p = d.get("state"), d.get("extractProgress") or {}
            if state == "done":
                result_url = (d.get("resultUrl") or {}).get("jsonUrl")
                if not isinstance(result_url, str) or not result_url:
                    raise ParseError("PaddleOCR 返回结果缺少下载地址")
                return result_url
            if state == "failed":
                raise ParseError(_classify_paddle_error(str(d.get("errorMsg") or "")))
            done, total = p.get("extractedPages"), p.get("totalPages")
            if state == "running" and isinstance(done, int) and isinstance(total, int) and total:
                status: tuple[str, float | None] = (f"PaddleOCR 正在解析，第 {done}/{total} 页", done / total)
            else:
                status = ("PaddleOCR 排队中", None)
            if status != last:
                progress(*status)
                last = status
            await asyncio.sleep(self._poll_interval)
        raise ParseError("PaddleOCR 解析超时")


async def _join_task[T](task: asyncio.Task[T]) -> None:
    """等上传子任务真正退出后再关闭文件句柄或重试，防止句柄泄露。"""
    joined = asyncio.gather(task, return_exceptions=True)
    re_cancelled = False
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            re_cancelled = True
    if re_cancelled:
        raise asyncio.CancelledError


def _safe(name: str) -> str:
    return re.sub(r'[\r\n"]', "_", name) or "document.pdf"


def _classify_paddle_error(msg: str) -> str:
    lower = msg.lower()
    if any(k in lower for k in ("token", "unauthorized", "auth", "过期", "无效", "权限")):
        return "PaddleOCR Token 无效或权限不足"
    if any(k in lower for k in ("quota", "limit", "额度", "配额", "频繁")):
        return "PaddleOCR 额度不足或请求过于频繁，请稍后再试"
    if any(k in lower for k in ("size", "page", "format", "corrupt", "invalid", "坏文件", "损坏", "过大", "不支持")):
        return "PaddleOCR 解析失败：PDF 文件损坏、过大或格式不受支持"
    return "PaddleOCR 解析失败：解析服务返回异常"


def _check(r: httpx2.Response, action: str, parse: bool = True) -> Any:
    if r.status_code in (401, 403):
        raise ParseError("PaddleOCR Token 无效或已过期")
    if 300 <= r.status_code < 400:
        raise ParseError(f"PaddleOCR {action}失败：收到不安全的重定向（HTTP {r.status_code}）")
    if r.status_code == 429:
        raise ParseError("PaddleOCR 今日免费额度已用完或请求过于频繁，请稍后再试，或改用 MinerU")
    if r.status_code >= 400:
        raise ParseError(f"PaddleOCR {action}失败：HTTP {r.status_code}", retryable=r.status_code >= 500)
    if not parse or "json" not in r.headers.get("content-type", "").lower():
        return {}
    try:
        return r.json()
    except ValueError as exc:
        raise ParseError(f"PaddleOCR {action}失败：解析服务返回格式异常") from exc
