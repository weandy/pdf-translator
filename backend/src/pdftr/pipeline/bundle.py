"""批量下载的短效令牌、快照预检与并发资源管理。"""

import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from pdftr.domain.zip_names import zip_filenames
from pdftr.pipeline.runner import ExportResult, Pipeline
from pdftr.pipeline.usecases import (
    Conflict,
    NotFound,
    ServiceBusy,
    UserError,
    export_crc32,
    get_job,
    published_export,
    write_export_meta,
)
from pdftr.pipeline.zip_stream import ZIP32_LIMIT, ZipEntry, zip_size

BUNDLE_TOKEN_TTL_S = 180
MAX_BUNDLE_TOKENS = 128
MAX_CONCURRENT_BUNDLE_STREAMS = 3
BUNDLE_STREAM_MAX_DURATION_S = 600
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
BUNDLE_FILENAME = "translations.zip"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")


@dataclass(frozen=True)
class BundleItem:
    job_id: str
    mode: Literal["mono", "dual"]
    side: Literal["left", "right"]
    snapshot: str


@dataclass(frozen=True)
class BundleEntry:
    item: BundleItem
    name: bytes
    size_bytes: int


@dataclass(frozen=True)
class BundleTicket:
    entries: tuple[BundleEntry, ...]
    size_bytes: int
    expires_at: float


class StaleBundle(Conflict):
    def __init__(self, indexes: list[int]) -> None:
        super().__init__("部分导出快照已失效，请重新准备")
        self.indexes = indexes


class BundleRegistry:
    def __init__(self) -> None:
        self._tickets: dict[str, BundleTicket] = {}
        self._lock = threading.Lock()
        self._streams = threading.BoundedSemaphore(MAX_CONCURRENT_BUNDLE_STREAMS)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdftr-zip")

    def issue(self, entries: tuple[BundleEntry, ...], size_bytes: int) -> str:
        with self._lock:
            now = time.monotonic()
            self._tickets = {key: ticket for key, ticket in self._tickets.items() if ticket.expires_at > now}
            if len(self._tickets) >= MAX_BUNDLE_TOKENS:
                raise ServiceBusy("当前打包请求较多，请稍后重试")
            token = secrets.token_urlsafe(24)
            while token in self._tickets:
                token = secrets.token_urlsafe(24)
            self._tickets[token] = BundleTicket(entries, size_bytes, now + BUNDLE_TOKEN_TTL_S)
            return token

    def claim(self, token: str) -> BundleTicket:
        with self._lock:
            ticket = self._tickets.pop(token, None)
            if ticket is None or ticket.expires_at <= time.monotonic():
                raise NotFound("下载链接已失效，请重新打包")
            return ticket

    @contextmanager
    def stream_slot(self) -> Iterator[None]:
        if not self._streams.acquire(blocking=False):
            raise ServiceBusy("当前打包下载较多，请稍后重试")
        try:
            yield
        finally:
            self._streams.release()

    def close(self) -> None:
        with self._lock:
            self._tickets.clear()
        self.pool.shutdown()


def _access(stack: ExitStack, pipeline: Pipeline, items: list[BundleItem]) -> None:
    for job_id in sorted({item.job_id for item in items}):
        if not stack.enter_context(pipeline.export_access(job_id)):
            raise NotFound("任务不存在")
        get_job(pipeline, job_id)


def _published(pipeline: Pipeline, item: BundleItem) -> ExportResult:
    return published_export(pipeline, item.job_id, item.mode, item.side, item.snapshot)


def issue_bundle(pipeline: Pipeline, registry: BundleRegistry, items: list[BundleItem]) -> tuple[str, int]:
    with ExitStack() as stack:
        _access(stack, pipeline, items)
        results = []
        stale = []
        for index, item in enumerate(items):
            try:
                results.append(_published(pipeline, item))
            except (NotFound, OSError):
                stale.append(index)
        if stale:
            raise StaleBundle(stale)
        names = zip_filenames([result.filename for result in results])
        entries = tuple(
            BundleEntry(item, name.encode("utf-8"), result.size_bytes)
            for item, name, result in zip(items, names, results, strict=True)
        )
        size_bytes = zip_size([(entry.name, entry.size_bytes) for entry in entries])
        if sum(e.size_bytes for e in entries) > MAX_BUNDLE_BYTES or size_bytes >= ZIP32_LIMIT:
            raise UserError("单个压缩包最多包含 512 MiB 的 PDF")
        return registry.issue(entries, size_bytes), size_bytes


def open_bundle(stack: ExitStack, pipeline: Pipeline, ticket: BundleTicket) -> list[ZipEntry]:
    _access(stack, pipeline, [entry.item for entry in ticket.entries])
    files = []
    for index, entry in enumerate(ticket.entries):
        try:
            result = _published(pipeline, entry.item)
            file = stack.enter_context(result.path.open("rb"))
            if result.size_bytes != entry.size_bytes or os.fstat(file.fileno()).st_size != entry.size_bytes:
                raise StaleBundle([index])
            files.append((entry, result, file))
        except (OSError, NotFound) as exc:
            raise StaleBundle([index]) from exc
    opened = []
    for index, (entry, result, file) in enumerate(files):
        crc = result.crc32
        if crc is None:
            try:
                crc = export_crc32(file)
            except OSError as exc:
                raise StaleBundle([index]) from exc
            # 并发下载可以各自补算，写入相同的原子提交标记。
            with suppress(OSError):
                write_export_meta(result.path, result.overflow_pages, result.size_bytes, crc)
        opened.append(ZipEntry(entry.name, entry.size_bytes, crc, file))
    return opened
