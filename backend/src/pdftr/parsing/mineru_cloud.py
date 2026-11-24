"""MinerU 精准解析 API 适配器（https://mineru.net）。

流程（S2 验证）：申请上传链接 → PUT 上传（预签名地址，不带鉴权头）→ 轮询批次结果 → 下载 Zip → 读 layout.json。
免费额度：每天 1000 页最高优先级，超出后排队变慢；单文件 ≤200 MB、≤200 页。
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import shutil
import socket
import tempfile
import time
import uuid
import zipfile
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Iterator
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from pdftr.domain.layout import LayoutResult
from pdftr.parsing.mineru_normalize import normalize_checked
from pdftr.parsing.mineru_plan import ParsePlan, ShardSpec, build_parse_plan, materialize_shards
from pdftr.parsing.port import ParseError, ParseProgress

API = "https://mineru.net/api/v4"
UPLOAD_CHUNK = 64 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_LAYOUT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_LAYOUT_BYTES = 96 * 1024 * 1024
MIN_TMP_FREE_BYTES = 128 * 1024 * 1024
MAX_SHARD_RESUBMIT = 1
_PERMANENT_ERRORS = ("坏文件", "加密", "损坏", "页数", "invalid pdf", "format")
ADAPTIVE_FAST_INTERVAL = 1.0
ADAPTIVE_SLOW_INTERVAL = 3.0
ADAPTIVE_BACKOFF_AFTER = 30.0
ADAPTIVE_JITTER_MAX = 0.15

log = logging.getLogger(__name__)
_mineru_request = ContextVar("mineru_request", default=False)


def _without_mineru_request_details(record: logging.LogRecord) -> bool:
    # httpx2 默认在 INFO 输出完整签名 URL，解析任务仅保留自身的结构化日志。
    return not (_mineru_request.get() and isinstance(record.msg, str) and record.msg.startswith("HTTP Request:"))


logging.getLogger("httpx2").addFilter(_without_mineru_request_details)


async def _join_task[T](task: asyncio.Future[T]) -> None:
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


async def _run_thread_joined[T](call: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _join_task(task)
        raise


async def _run_tasks[T](calls: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    tasks = [asyncio.create_task(call) for call in calls]
    batch = asyncio.gather(*tasks)
    try:
        return await asyncio.shield(batch)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await _join_task(asyncio.gather(batch, *tasks, return_exceptions=True))
        raise


class MinerUResourceLimits:
    """每个服务进程内的解析器共享传输上限，本地重任务复用流水线名额。"""

    def __init__(
        self,
        *,
        max_global_uploads: int = 4,
        max_global_downloads: int = 2,
        max_per_job_uploads: int = 2,
        max_per_job_downloads: int = 2,
        local_slot: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self.global_uploads = asyncio.Semaphore(max_global_uploads)
        self.global_downloads = asyncio.Semaphore(max_global_downloads)
        self.max_per_job_uploads = max_per_job_uploads
        self.max_per_job_downloads = max_per_job_downloads
        fallback = asyncio.Semaphore(1)
        self.local_slot = local_slot if local_slot is not None else lambda: fallback


@dataclass(frozen=True)
class _RemoteShard:
    spec: ShardSpec
    source: Path
    name: str
    data_id: str

    def file_item(self) -> dict[str, str]:
        item = {"name": self.name, "data_id": self.data_id}
        if self.spec.page_ranges is not None:
            item["page_ranges"] = self.spec.page_ranges
        return item


@dataclass
class _LayoutBudget:
    consumed: int = 0


class MinerUCloudParser:
    name = "mineru-cloud"
    is_cloud = True

    def __init__(
        self,
        token: str,
        *,
        model_version: str = "vlm",
        language: str = "en",
        poll_interval: float | None = None,
        initial_poll_delay: float = 4.0,
        timeout: float = 300.0,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        stall_timeout: float = 60.0,
        proxy: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        use_pdf_parent_tmp: bool = False,
        enable_partial_pages: bool = False,
        enable_sharding: bool = False,
        shard_threshold: int = 24,
        shard_target_pages: int = 14,
        shard_min_pages: int = 8,
        max_shards: int = 6,
        limits: MinerUResourceLimits | None = None,
    ) -> None:
        if not token:
            raise ParseError("未配置 MinerU Token")
        self._token = token
        self._model_version = model_version
        self._language = language
        self._poll_interval = poll_interval
        self._initial_poll_delay = initial_poll_delay
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._stall_timeout = stall_timeout
        # 只有发往 MinerU 的请求走代理（服务器到国内线路差时用，见 PDFTR_MINERU_PROXY）；有注入的传输层时不用
        self._proxy = None if transport is not None else (proxy or None)
        self._use_pdf_parent_tmp = use_pdf_parent_tmp
        self._enable_partial_pages = enable_partial_pages
        self._enable_sharding = enable_sharding
        self._shard_threshold = shard_threshold
        self._shard_target_pages = shard_target_pages
        self._shard_min_pages = shard_min_pages
        self._max_shards = max_shards
        self._limits = limits if limits is not None else MinerUResourceLimits()
        self._transport = transport  # 测试时注入假传输层

    @property
    def cache_config_sig(self) -> str:
        return f"model={quote(self._model_version, safe='')}|lang={quote(self._language, safe='')}|formula=1|table=1"

    async def parse(
        self,
        pdf: Path,
        pages: list[int] | None = None,
        raw_dir: Path | None = None,
        on_progress: ParseProgress | None = None,
    ) -> LayoutResult:
        log_context = _mineru_request.set(True)
        try:
            with self._attempt_dir(pdf, raw_dir) as attempt_dir:
                return await self._parse(pdf, pages, raw_dir, on_progress or _ignore, attempt_dir=attempt_dir)
        except httpx2.TransportError as e:
            raise ParseError(
                "无法连接 MinerU 云端，请检查服务器网络（Docker 部署时确认容器能访问外网）", retryable=True
            ) from e
        finally:
            _mineru_request.reset(log_context)

    @contextlib.contextmanager
    def _attempt_dir(self, pdf: Path, raw_dir: Path | None) -> Iterator[Path | None]:
        if raw_dir is not None or not self._use_pdf_parent_tmp:
            if self._enable_partial_pages or self._enable_sharding:
                if raw_dir is not None:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=raw_dir, prefix="mineru-attempt-") as path:
                    yield Path(path)
            else:
                yield None
            return
        parent = pdf.parent / "parser-tmp"
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or parent.resolve() != pdf.parent.resolve() / "parser-tmp":
            raise ParseError("MinerU 临时目录超出任务目录")
        attempt = parent / f"attempt-{uuid.uuid4().hex}"
        attempt.mkdir(mode=0o700)
        try:
            yield attempt
        finally:
            shutil.rmtree(attempt, ignore_errors=True)
            with contextlib.suppress(OSError):
                parent.rmdir()

    async def _parse(
        self,
        pdf: Path,
        pages: list[int] | None,
        raw_dir: Path | None,
        on_progress: ParseProgress,
        *,
        attempt_dir: Path | None = None,
    ) -> LayoutResult:
        started = time.perf_counter()
        deadline_at = time.monotonic() + self._timeout
        try:
            async with asyncio.timeout(self._remaining(deadline_at)):
                async with self._limits.local_slot():
                    if self._remaining(deadline_at) <= 0:
                        raise TimeoutError
                    plan, sources = await _run_thread_joined(lambda: self._plan(pdf, pages, attempt_dir))
        except TimeoutError:
            raise ParseError("MinerU 规划超过本次解析总时限", retryable=True) from None
        plan_s = time.perf_counter() - started
        timings: dict[str, float] = dict.fromkeys(("req", "upload", "wait", "download", "norm"), 0.0)
        headers = {"Authorization": f"Bearer {self._token}"}
        hooks = None
        if self._transport is None:
            hooks = {"request": [partial(_validate_presigned_request, resolve_dns=self._proxy is None)]}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(120, connect=15),
                               transport=self._transport, proxy=self._proxy) as api,
            httpx2.AsyncClient(timeout=httpx2.Timeout(300, connect=15), transport=self._transport,
                               proxy=self._proxy, follow_redirects=True, event_hooks=hooks) as plain,
        ):  # fmt: skip
            try:
                async with asyncio.timeout(self._remaining(deadline_at)):
                    result, resubmits = await self._execute_plan(
                        api, plain, plan, sources, raw_dir, attempt_dir, deadline_at, on_progress, timings
                    )
            except (TimeoutError, httpx2.TimeoutException) as e:
                if isinstance(e, TimeoutError) or self._remaining(deadline_at) <= 0:
                    message = "MinerU 解析超过本次解析总时限"
                else:
                    message = "连接 MinerU 云端超时，请检查服务器能否访问 mineru.net"
                raise ParseError(message, retryable=True, max_retries=1 if len(plan.shards) > 1 else None) from None
        log.info(
            "MinerU 解析完成 mode=%s pages=%d shards=%d resubmits=%d "
            "plan_s=%.2f req_s=%.2f upload_s=%.2f wait_s=%.2f download_s=%.2f norm_s=%.2f total_s=%.2f",
            plan.mode,
            plan.total_selected_pages if plan.total_selected_pages is not None else len(result.pages),
            len(plan.shards),
            resubmits,
            plan_s,
            timings["req"],
            timings["upload"],
            timings["wait"],
            timings["download"],
            timings["norm"],
            time.perf_counter() - started,
        )
        return result

    async def _execute_plan(
        self,
        api: httpx2.AsyncClient,
        plain: httpx2.AsyncClient,
        plan: ParsePlan,
        sources: tuple[Path, ...],
        raw_dir: Path | None,
        attempt_dir: Path | None,
        deadline_at: float,
        on_progress: ParseProgress,
        timings: dict[str, float],
    ) -> tuple[LayoutResult, int]:
        pending = list(zip(plan.shards, sources, strict=True))
        multi = len(pending) > 1
        budget = _LayoutBudget() if multi else None
        uploads = asyncio.Semaphore(self._limits.max_per_job_uploads)
        downloads = asyncio.Semaphore(self._limits.max_per_job_downloads)
        completed: dict[int, LayoutResult] = {}
        legacy = plan.mode == "full_single" and not self._enable_sharding
        for resubmits in range(MAX_SHARD_RESUBMIT + 1):
            tag = uuid.uuid4().hex[:8]
            batch = [
                _RemoteShard(
                    shard,
                    source,
                    source.name if legacy else f"doc_{shard.index}.pdf",
                    source.stem[:128] if legacy else f"p{tag}_{shard.index}",
                )
                for shard, source in pending
            ]
            upload_progress = _stage_progress(on_progress)
            upload_progress("正在上传到 MinerU 云端", 0.0)
            req_started = time.perf_counter()
            batch_id, urls = await self._request_upload(api, batch, deadline_at=deadline_at)
            upload_started = time.perf_counter()
            timings["req"] += upload_started - req_started
            bytes_by_id = {item.spec.index: item.source.stat().st_size for item in batch}
            total_bytes = sum(bytes_by_id.values())
            sent: dict[int, float] = {}

            async def upload(
                item: _RemoteShard,
                url: str,
                *,
                sent=sent,
                bytes_by_id=bytes_by_id,
                total_bytes=total_bytes,
                upload_progress=upload_progress,
            ) -> float:
                index = item.spec.index

                def on_upload(message: str, fraction: float | None = None) -> None:
                    if fraction is not None:
                        sent[index] = fraction * bytes_by_id[index]
                        fraction = sum(sent.values()) / max(1, total_bytes)
                    upload_progress(message, fraction)

                async with uploads, self._limits.global_uploads:
                    response = await self._upload(plain, url, item.source, on_upload, deadline_at=deadline_at)
                    _check(response, "上传文件", presigned=True, read_json=False)
                    sent[index] = bytes_by_id[index]
                    return time.monotonic()

            put_done_at = await _run_tasks(upload(item, url) for item, url in zip(batch, urls, strict=True))
            wait_started = time.perf_counter()
            upload_s = wait_started - upload_started
            timings["upload"] += upload_s
            uploaded = upload_summary(total_bytes, upload_s)

            def with_upload(message: str, fraction: float | None = None, *, uploaded: str = uploaded) -> None:
                on_progress(f"{message}（{uploaded}）", fraction)

            done, failure = await self._wait_batch(
                api,
                batch_id,
                _stage_progress(with_upload),
                deadline_at=deadline_at,
                first_put_done_at=min(put_done_at),
                batch={item.data_id: item for item in batch},
                multi=multi,
                previous_pages=sum(len(shard.orig_pages) for shard in plan.shards if shard.index in completed),
                previous_shards=len(completed),
            )
            timings["wait"] += time.perf_counter() - wait_started
            if done:
                on_progress("正在下载解析结果", 0.0 if failure is None else None)
                to_download = [(item.spec, done[item.spec.index]) for item in batch if item.spec.index in done]

                async def download(shard: ShardSpec, url: str) -> LayoutResult:
                    async with downloads:
                        return await self._download(
                            plain,
                            url,
                            None if multi else raw_dir,
                            deadline_at,
                            timings=timings,
                            attempt_dir=attempt_dir,
                            shard=shard,
                            strict_relative=plan.mode != "full_single",
                            budget=budget,
                        )

                download_started = time.perf_counter()
                normalized_before = timings["norm"]
                results = await _run_tasks(download(shard, url) for shard, url in to_download)
                # 整段只计一次墙钟耗时，扣除单列的本机归一化时间。
                timings["download"] += time.perf_counter() - download_started - (timings["norm"] - normalized_before)
                completed.update((shard.index, result) for (shard, _), result in zip(to_download, results, strict=True))
            if failure is None:
                result = _merge_shards(plan, [completed[i] for i in sorted(completed)])
                on_progress("下载解析结果完成", 1.0)
                return result, resubmits
            if resubmits >= MAX_SHARD_RESUBMIT or self._remaining(deadline_at) < 60.0:
                raise ParseError(f"MinerU 分片解析失败：{failure}", retryable=True, max_retries=1)
            pending = [(shard, path) for shard, path in pending if shard.index not in completed]
            on_progress("MinerU 正在重试未完成的分片")
            await asyncio.sleep(min(3.0, self._remaining(deadline_at)))
        raise AssertionError("无法到达")

    def _plan(self, pdf: Path, pages: list[int] | None, attempt_dir: Path | None) -> tuple[ParsePlan, tuple[Path, ...]]:
        plan = build_parse_plan(
            pdf,
            pages,
            enable_partial_pages=self._enable_partial_pages,
            enable_sharding=self._enable_sharding,
            shard_threshold=self._shard_threshold,
            shard_target_pages=self._shard_target_pages,
            shard_min_pages=self._shard_min_pages,
            max_shards=self._max_shards,
        )
        if attempt_dir is not None:
            return materialize_shards(pdf, plan, attempt_dir)
        return plan, (pdf,)

    async def _download(
        self,
        client: httpx2.AsyncClient,
        url: str,
        raw_dir: Path | None,
        deadline_at: float,
        *,
        timings: dict[str, float] | None = None,
        attempt_dir: Path | None = None,
        shard: ShardSpec | None = None,
        strict_relative: bool = False,
        budget: _LayoutBudget | None = None,
    ) -> LayoutResult:
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=attempt_dir or raw_dir, prefix="mineru-", suffix=".part", delete=False
            ) as f:
                tmp = Path(f.name)
                async with asyncio.timeout(self._remaining(deadline_at)), self._limits.global_downloads:
                    for delay in (*self._retry_delays, None):
                        try:
                            async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                                if response.status_code >= 500 and delay is not None:
                                    raise httpx2.TransportError("MinerU 下载端暂时不可用")
                                _check(response, "下载解析结果", read_json=False, presigned=True)
                                content_length = response.headers.get("Content-Length", "")
                                if (
                                    content_length.isascii()
                                    and content_length.isdecimal()
                                    and int(content_length) > MAX_ARCHIVE_BYTES
                                ):
                                    raise ParseError("MinerU 解析结果 ZIP 超过大小上限")
                                size = 0
                                next_disk_check = 0
                                async for chunk in response.aiter_bytes(UPLOAD_CHUNK):
                                    size += len(chunk)
                                    if size > MAX_ARCHIVE_BYTES:
                                        raise ParseError("MinerU 解析结果 ZIP 超过大小上限")
                                    if attempt_dir is not None and size >= next_disk_check:
                                        if shutil.disk_usage(attempt_dir).free < MIN_TMP_FREE_BYTES:
                                            raise ParseError("MinerU 下载临时空间不足，请释放磁盘空间")
                                        next_disk_check = size + 4 * 1024 * 1024
                                    f.write(chunk)
                            break
                        except httpx2.TransportError as e:
                            if delay is None:
                                raise
                            f.seek(0)
                            f.truncate()
                            log.warning("下载 MinerU 结果出错（%s），%.0f 秒后重试", type(e).__name__, delay)
                            await asyncio.sleep(min(delay, self._remaining(deadline_at)))
            archive = tmp
            async with asyncio.timeout(self._remaining(deadline_at)), self._limits.local_slot():
                normalize_started = time.perf_counter()
                result = await _run_thread_joined(
                    lambda: _normalize_archive(
                        archive, (shard.orig_pages or None) if shard else None, strict_relative, budget
                    )
                )
                if timings is not None:
                    timings["norm"] += time.perf_counter() - normalize_started
                if raw_dir is None:
                    tmp.unlink()
                    tmp = None
            if raw_dir is not None:
                archive.replace(raw_dir / "mineru.zip")
            return result
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def _request_upload(
        self, api: httpx2.AsyncClient, batch: list[_RemoteShard], *, deadline_at: float
    ) -> tuple[str, list[str]]:
        r = await self._send(
            api,
            "POST",
            f"{API}/file-urls/batch",
            deadline=self._remaining(deadline_at),
            json={
                "files": [item.file_item() for item in batch],
                "model_version": self._model_version,
                "enable_formula": True,
                "enable_table": True,
                "language": self._language,
            },
        )
        body = _check(r, "申请上传链接")
        data = body.get("data")
        batch_id = data.get("batch_id") if isinstance(data, dict) else None
        urls = data.get("file_urls") if isinstance(data, dict) else None
        if (
            not isinstance(batch_id, str)
            or not batch_id.strip()
            or batch_id in (".", "..")
            or not isinstance(urls, list)
            or len(urls) != len(batch)
            or any(not isinstance(url, str) or not url.strip() for url in urls)
            or len(set(urls)) != len(urls)
        ):
            raise ParseError("MinerU 申请上传链接返回格式异常")
        if self._transport is None:
            for url in urls:
                _presigned_host(url)
        return batch_id, urls

    async def _wait(
        self,
        api: httpx2.AsyncClient,
        batch_id: str,
        on_progress: ParseProgress,
        *,
        deadline_at: float | None = None,
        first_put_done_at: float | None = None,
    ) -> str:
        deadline = deadline_at if deadline_at is not None else time.monotonic() + self._timeout
        done, _ = await self._wait_batch(
            api,
            batch_id,
            on_progress,
            deadline_at=deadline,
            first_put_done_at=first_put_done_at,
            batch={},
            multi=False,
        )
        return done[0]

    async def _wait_batch(
        self,
        api: httpx2.AsyncClient,
        batch_id: str,
        on_progress: ParseProgress,
        *,
        deadline_at: float,
        first_put_done_at: float | None,
        batch: dict[str, _RemoteShard],
        multi: bool,
        previous_pages: int = 0,
        previous_shards: int = 0,
    ) -> tuple[dict[int, str], str | None]:
        last = None
        idle_since: float | None = None
        last_pages: dict[int, int] = {}
        done: dict[int, str] = {}
        failed: dict[int, str] = {}
        failure_deadline: float | None = None
        expected_count = len(batch) or 1
        pages_by_shard = {item.spec.index: len(item.spec.orig_pages) for item in batch.values()}
        await self._wait_before_first_poll(first_put_done_at, deadline_at)
        while (remaining := self._remaining(deadline_at)) > 0:
            r = await self._send(
                api,
                "GET",
                f"{API}/extract-results/batch/{quote(batch_id, safe='')}",
                deadline=min(180.0, remaining),
            )
            body = _check(r, "查询解析进度")
            data = body.get("data")
            items = _align_batch(data.get("extract_result") if isinstance(data, dict) else None, batch)
            active = False
            current: dict[int, dict] = {}
            for index, item in items.items():
                if index in done or index in failed:
                    continue
                state = item.get("state")
                if state == "done":
                    url = item.get("full_zip_url")
                    if not isinstance(url, str) or not url.strip():
                        raise ParseError("MinerU 解析结果缺少下载链接")
                    if self._transport is None:
                        _presigned_host(url)
                    done[index] = url
                    active = True
                    continue
                if state == "failed":
                    reason = _safe_error(item.get("err_msg"), self._token)
                    if not multi or reason in _PERMANENT_ERRORS:
                        raise ParseError(f"MinerU 解析失败：{reason}")
                    failed[index] = reason
                    continue
                progress = item.get("extract_progress")
                progress = progress if isinstance(progress, dict) else {}
                pages = progress.get("extracted_pages")
                if type(pages) is int:
                    active |= pages > last_pages.get(index, 0)
                    last_pages[index] = max(pages, last_pages.get(index, 0))
                active |= state in ("running", "converting")
                if state in ("running", "converting") and index in last_pages:
                    item = {**item, "extract_progress": {**progress, "extracted_pages": last_pages[index]}}
                current[index] = item
            if len(done) == expected_count:
                return done, None
            if failed:
                now = time.monotonic()
                if failure_deadline is None:
                    failure_deadline = min(now + 15.0, deadline_at)
                if len(done) + len(failed) == expected_count or now >= failure_deadline or not current:
                    return done, next(iter(failed.values()))
            status = _batch_status(
                current, done, pages_by_shard, previous_pages=previous_pages, previous_shards=previous_shards
            )
            if status != last:
                on_progress(*status)
                last = status
            next_poll_deadline = min(deadline_at, failure_deadline) if failure_deadline is not None else deadline_at
            idle_since = await self._wait_before_next_poll(active, idle_since, next_poll_deadline)
            if failure_deadline is not None and time.monotonic() >= failure_deadline:
                return done, next(iter(failed.values()))
        on_progress("MinerU 云端解析等待超过 5 分钟，准备重新提交", None)
        raise ParseError("MinerU 解析超过 5 分钟，正在自动重试", retryable=True, max_retries=1 if multi else None)

    async def _wait_before_first_poll(self, first_put_done_at: float | None, deadline_at: float) -> None:
        if self._poll_interval is None and first_put_done_at is not None:
            wait = min(self._initial_poll_delay - (time.monotonic() - first_put_done_at), self._remaining(deadline_at))
            if wait > 0:
                await asyncio.sleep(wait)

    async def _wait_before_next_poll(self, active: bool, idle_since: float | None, deadline_at: float) -> float | None:
        now = time.monotonic()
        remaining = deadline_at - now
        if remaining <= 0:
            return idle_since
        interval = self._poll_interval
        if interval is None:
            idle_since = None if active else (now if idle_since is None else idle_since)
            backoff = idle_since is not None and now - idle_since >= ADAPTIVE_BACKOFF_AFTER
            interval = ADAPTIVE_SLOW_INTERVAL if backoff else ADAPTIVE_FAST_INTERVAL
            interval += random.uniform(0.0, ADAPTIVE_JITTER_MAX)
        await asyncio.sleep(min(interval, remaining))
        return idle_since

    async def _send(
        self, client: httpx2.AsyncClient, method: str, url: str, *, deadline: float = 180, **kwargs: Any
    ) -> httpx2.Response:
        """网络偶发中断及查询端 5xx 时按退避间隔重试。

        deadline 是这次请求（包括内部重试）的总时限，不能因为重试而重复累加。
        """
        deadline_at = time.monotonic() + deadline
        for delay in (*self._retry_delays, None):
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise httpx2.TimeoutException(f"{method} 超过 {deadline:.0f} 秒未完成")
            try:
                response = await asyncio.wait_for(client.request(method, url, **kwargs), remaining)
                if method == "GET" and response.status_code >= 500 and delay is not None:
                    raise httpx2.TransportError("MinerU 查询端暂时不可用")
                return response
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
    return "MinerU 已提交任务，正在等待状态更新", None


def _presigned_host(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
        if (
            parsed.scheme != "https"
            or not host
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)
            or chr(92) in url
        ):
            raise ValueError
        host_without_dot = host.rstrip(".")
        try:
            address = ip_address(host_without_dot)
        except ValueError:
            try:
                address = ip_address(socket.inet_aton(host_without_dot))
            except OSError:
                address = None
        if address is None:
            if "." not in host_without_dot or host_without_dot.endswith((".localhost", ".local", ".internal")):
                raise ValueError
        elif not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError
        httpx2.URL(url)
        return host, port
    except (ValueError, httpx2.InvalidURL):
        raise ParseError("MinerU 返回了不安全的临时传输链接") from None


async def _validate_presigned_request(request: httpx2.Request, *, resolve_dns: bool = True) -> None:
    host, port = _presigned_host(str(request.url))
    if not resolve_dns:
        # SOCKS 与 HTTP 代理自行解析目标域名，转发前仍拒绝不安全的 URL 字面量。
        return
    try:
        addresses = await _run_thread_joined(lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except OSError:
        raise httpx2.ConnectError("MinerU 临时传输域名无法解析") from None
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        for address in (ip_address(str(info[4][0])) for info in addresses)
    ):
        raise ParseError("MinerU 返回了不安全的临时传输链接")


def _align_batch(items: object, batch: dict[str, _RemoteShard]) -> dict[int, dict]:
    if not isinstance(items, list):
        raise ParseError("MinerU 返回批次项与请求不一致")
    aligned: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ParseError("MinerU 返回批次项与请求不一致")
        data_id = item.get("data_id")
        if len(batch) <= 1 and len(items) == 1 and (data_id in (None, "") or not batch):
            match = next(iter(batch.values()), None)
        elif isinstance(data_id, str) and data_id in batch:
            match = batch[data_id]
        else:
            raise ParseError("MinerU 返回批次项与请求不一致")
        index = match.spec.index if match is not None else 0
        if index in aligned or (match is not None and "file_name" in item and item["file_name"] != match.name):
            raise ParseError("MinerU 返回批次项与请求不一致")
        aligned[index] = item
    return aligned


def _stage_progress(callback: ParseProgress) -> ParseProgress:
    highest = 0.0

    def emit(message: str, fraction: float | None = None) -> None:
        nonlocal highest
        if fraction is not None:
            highest = max(highest, min(fraction, 1.0))
        callback(message, highest)

    return emit


def _batch_status(
    current: dict[int, dict],
    done: dict[int, str],
    pages_by_shard: dict[int, int],
    *,
    previous_pages: int,
    previous_shards: int,
) -> tuple[str, float | None]:
    total_shards = max(1, len(pages_by_shard)) + previous_shards
    if total_shards == 1 and current:
        first = next(iter(current.values()))
        progress = first.get("extract_progress")
        return _describe(first.get("state"), progress if isinstance(progress, dict) else {})
    states = [item.get("state") for item in current.values()]
    finished = previous_shards + len(done)
    if finished or any(state in ("running", "converting") for state in states):
        total_pages = previous_pages + sum(pages_by_shard.values())
        extracted = previous_pages + sum(pages_by_shard[index] for index in done)
        for index, item in current.items():
            progress = item.get("extract_progress")
            pages = progress.get("extracted_pages") if isinstance(progress, dict) else None
            if item.get("state") in ("running", "converting") and type(pages) is int:
                extracted += min(max(pages, 0), pages_by_shard[index])
        fraction = extracted / total_pages
        return (
            f"MinerU 正在解析，第 {extracted}/{total_pages} 页（{fraction:.0%}），已完成 {finished}/{total_shards} 片",
            fraction,
        )
    return _describe("pending" if "pending" in states else "waiting-file" if "waiting-file" in states else None, {})


def _safe_error(value: object, token: str) -> str:
    if not isinstance(value, str):
        return "未知原因"
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    if token:
        value = value.replace(token, "[已隐去]")
    value = re.sub(r"(?:https?://|www\.)\S+", "[已隐去链接]", value, flags=re.IGNORECASE)
    # 只保留已知错误分类，避免供应商正文、文件名或签名参数扩散。
    for phrase in _PERMANENT_ERRORS:
        pattern = rf"\b{re.escape(phrase)}\b" if phrase.isascii() else re.escape(phrase)
        if re.search(pattern, value, re.IGNORECASE):
            return phrase
    return "云端未返回可公开的错误原因"


def _check(r: httpx2.Response, action: str, *, read_json: bool = True, presigned: bool = False) -> dict:
    if r.status_code in (401, 403):
        if presigned:
            raise ParseError("MinerU 临时传输链接已过期或无权限，请重试", retryable=True)
        raise ParseError("MinerU Token 无效或已过期")
    if r.status_code == 429:
        raise ParseError("MinerU 请求过于频繁或额度不足，请稍后再试")
    if r.status_code >= 400:
        raise ParseError(f"MinerU {action}失败：HTTP {r.status_code}", retryable=r.status_code >= 500)
    if not read_json or not r.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        body = r.json()
    except ValueError:
        raise ParseError(f"MinerU {action}返回格式异常") from None
    if not isinstance(body, dict):
        raise ParseError(f"MinerU {action}返回格式异常")
    if body.get("code", 0) != 0:
        raise ParseError(f"MinerU {action}失败，请检查云端服务与额度")
    return body


def _merge_shards(plan: ParsePlan, results: list[LayoutResult]) -> LayoutResult:
    if len(results) != len(plan.shards):
        raise ParseError("MinerU 解析分片不完整")
    pages = sorted((page for result in results for page in result.pages), key=lambda page: page.index)
    if len({page.index for page in pages}) != len(pages):
        raise ParseError("MinerU 解析分片包含重复页号")
    parser = results[0].parser
    if any(result.parser != parser for result in results):
        raise ParseError("MinerU 解析分片的解析器不一致")
    versions = {result.parser_version for result in results if result.parser_version}
    if len(versions) > 1:
        log.warning("MinerU 解析分片返回不同版本")
    return LayoutResult(
        parser=parser,
        parser_version=next((result.parser_version for result in results if result.parser_version), ""),
        pages=pages,
        requested_pages=list(plan.requested_pages) if plan.requested_pages is not None else None,
    )


def _normalize_archive(
    archive: Path,
    orig_pages: tuple[int, ...] | None = None,
    strict_relative: bool = False,
    budget: _LayoutBudget | None = None,
) -> LayoutResult:
    try:
        layout = _read_layout(archive) if budget is None else _read_layout(archive, budget)
        return normalize_checked(layout, orig_pages=orig_pages, strict_relative=strict_relative)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ParseError("MinerU 结果含非法页号、页面尺寸或版面结构") from None


def _read_layout(archive: Path, budget: _LayoutBudget | None = None) -> dict:
    try:
        with zipfile.ZipFile(archive) as z:
            info = next((item for item in z.infolist() if item.filename.endswith("layout.json")), None)
            if info is None:
                raise ParseError("MinerU 结果中缺少 layout.json")
            if info.file_size > MAX_LAYOUT_BYTES:
                raise ParseError("MinerU 结果的 layout.json 解压后超过大小上限")
            remaining = MAX_TOTAL_LAYOUT_BYTES - budget.consumed if budget is not None else MAX_LAYOUT_BYTES
            limit = min(MAX_LAYOUT_BYTES, remaining)
            if info.file_size > remaining:
                raise ParseError("MinerU 分片结果累计解压超过大小上限")
            with z.open(info) as layout:
                data = layout.read(limit + 1)
            if len(data) > MAX_LAYOUT_BYTES:
                raise ParseError("MinerU 结果的 layout.json 解压后超过大小上限")
            if len(data) > remaining:
                raise ParseError("MinerU 分片结果累计解压超过大小上限")
            if budget is not None:
                budget.consumed += len(data)
            parsed = json.loads(data)
            if not isinstance(parsed, dict):
                raise ParseError("MinerU 结果的 layout.json 格式异常")
            return parsed
    except ParseError:
        raise
    except (zipfile.BadZipFile, ValueError, RuntimeError) as e:
        raise ParseError("MinerU 结果 ZIP 或 layout.json 格式异常") from e
