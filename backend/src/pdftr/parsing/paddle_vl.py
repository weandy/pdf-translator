"""百度飞桨 PaddleOCR-VL 云端解析（星河社区 API，https://aistudio.baidu.com/paddleocr）。

流程：上传 PDF 提交任务 → 轮询 → 下载 JSONL 结果。结果只有块坐标（像素）与块内容，行内公式写成 $LaTeX$、
没有位置；这里只换算坐标、对照块类型，行与行内公式位置由融合阶段对照 PDF 字符推算（fusion/formula_align.py）。

与 MinerU 的对比见 docs/02-spikes.md：文字准确率相当、公式 LaTeX 更干净、解析更快、海外服务器不走代理也能用；
但没有行级与行内公式坐标，默认解析服务仍是 MinerU。
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pymupdf

from pdftr.domain.geometry import Rect
from pdftr.domain.ir import BlockType
from pdftr.domain.layout import LayoutBlock, LayoutPage, LayoutResult
from pdftr.parsing.mineru_cloud import upload_status
from pdftr.parsing.port import ParseError, ParseProgress

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"
CHUNK = 64 * 1024

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
            jsonl = await self._run(pdf.read_bytes(), pdf.name, progress)
        except httpx2.TimeoutException as e:
            raise ParseError("连接 PaddleOCR 云端超时，请检查服务器能否访问 aistudio-app.com", retryable=True) from e
        except httpx2.TransportError as e:
            raise ParseError("无法连接 PaddleOCR 云端，请检查服务器网络", retryable=True) from e
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "paddleocr.jsonl").write_text(jsonl, encoding="utf-8")
        return normalize(jsonl, sizes)

    async def _run(self, data: bytes, name: str, progress: ParseProgress) -> str:
        headers = {"Authorization": f"bearer {self._token}"}
        timeout = httpx2.Timeout(120, connect=15)
        async with (
            httpx2.AsyncClient(headers=headers, timeout=timeout, transport=self._transport, proxy=self._proxy) as api,
            # 结果地址是预签名的，不能带鉴权头，用单独的客户端
            httpx2.AsyncClient(timeout=timeout, transport=self._transport, proxy=self._proxy,
                               follow_redirects=True) as plain,
        ):  # fmt: skip
            progress("正在上传到 PaddleOCR 云端")
            job = await self._submit(api, data, name, progress)
            url = await self._wait(api, job, progress)
            progress("正在下载解析结果", 1.0)
            r = await plain.get(url)
            _check(r, "下载解析结果", parse=False)  # 结果是每行一页的 JSONL
            return r.text

    async def _submit(self, client: httpx2.AsyncClient, data: bytes, name: str, progress: ParseProgress) -> str:
        """上传并提交任务，每秒汇报进度；连续 stall_timeout 秒没有进展才重传（与 MinerU 一致）。"""
        options = {"useDocOrientationClassify": False, "useDocUnwarping": False, "useChartRecognition": False}
        boundary = "pdftr" + str(int(time.time() * 1000))
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{MODEL}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="optionalPayload"\r\n\r\n{json.dumps(options)}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{_safe(name)}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        body_bytes = head + data + tail
        attempts = len(self._retry_delays) + 1
        for n, delay in enumerate((*self._retry_delays, None), start=1):
            sent, start = 0, time.monotonic()
            last = start

            async def body() -> AsyncIterator[bytes]:
                nonlocal sent, last
                for i in range(0, len(body_bytes), CHUNK):
                    yield body_bytes[i : i + CHUNK]
                    sent, last = min(i + CHUNK, len(body_bytes)), time.monotonic()

            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                       "Content-Length": str(len(body_bytes))}  # fmt: skip
            task = asyncio.ensure_future(client.post(JOB_URL, content=body(), headers=headers))
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=1.0)
                    if done:
                        r = task.result()
                        return _check(r, "提交任务")["data"]["jobId"]
                    now = time.monotonic()
                    if now - last > self._stall_timeout:
                        raise TimeoutError
                    retry = f"（第 {n}/{attempts} 次尝试）" if n > 1 else ""
                    done_bytes = min(sent, len(data))
                    progress(upload_status(done_bytes, len(data), now - start, "PaddleOCR") + retry,
                             done_bytes / len(data))  # fmt: skip
            except (httpx2.TransportError, TimeoutError) as e:
                task.cancel()
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
            d = _check(await client.get(f"{JOB_URL}/{job}"), "查询解析进度")["data"]
            state, p = d.get("state"), d.get("extractProgress") or {}
            if state == "done":
                return d["resultUrl"]["jsonUrl"]
            if state == "failed":
                raise ParseError(f"PaddleOCR 解析失败：{d.get('errorMsg') or '未知原因'}")
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


def _safe(name: str) -> str:
    return re.sub(r'[\r\n"]', "_", name) or "document.pdf"


def _check(r: httpx2.Response, action: str, parse: bool = True) -> Any:
    if r.status_code in (401, 403):
        raise ParseError("PaddleOCR Token 无效或已过期")
    if r.status_code == 429:
        raise ParseError("PaddleOCR 今日免费额度已用完或请求过于频繁，请稍后再试，或改用 MinerU")
    if r.status_code >= 400:
        raise ParseError(f"PaddleOCR {action}失败：HTTP {r.status_code}", retryable=r.status_code >= 500)
    if not parse or "json" not in r.headers.get("content-type", ""):
        return {}
    return r.json()
