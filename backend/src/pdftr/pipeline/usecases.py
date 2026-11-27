"""接口层调用的用例：创建任务、查询、段落编辑与重翻、费用预估、公式裁剪图。

接口层（api）只做 HTTP 与这些函数之间的转换，不含业务规则。
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import zlib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel

from pdftr.domain.document_name import download_filename, paper_title, paper_title_blocks
from pdftr.domain.expansion import ExpansionOp
from pdftr.domain.ids import new_job_id
from pdftr.domain.ir import (
    BlockType,
    Document,
    OutlineItem,
    Quality,
    Revision,
    RevisionAction,
    Segment,
    SegmentStatus,
)
from pdftr.domain.job import STAGES, Job, JobOptions, JobStatus
from pdftr.domain.llm import ChatError, ChatOptions, ChatUsage
from pdftr.domain.submissions import SubmissionContext
from pdftr.domain.text import LETTER_HEADING, NUMBERED_HEADING, RICH_TAG, ROMAN_HEADING, normalize_term_key
from pdftr.extraction.chars import page_count
from pdftr.extraction.outline import pdf_outline
from pdftr.glossary.user_import import ParsedGlossary, parse_upload
from pdftr.pipeline.memory import release_memory
from pdftr.pipeline.runner import ExportResult, Pipeline, chat_session, load_context, load_document
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.versions import purge_versions, version_artifact
from pdftr.pipeline.workers import finish_on_cancel, run_thread
from pdftr.rendering.background import crop_block_png, crop_png
from pdftr.rendering.export import ExportMode, Side, export_pdf_to_path, page_links, place_links
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import PdfLink
from pdftr.storage.port import RetranslateConflict
from pdftr.translation.batching import placeholder_problems, rich_text_problems
from pdftr.translation.cost import Price, cost_cny, is_deepseek_peak
from pdftr.translation.segmenter import BAD_SOURCE_MESSAGE, bad_source
from pdftr.translation.service import TranslateOptions, translate

log = logging.getLogger(__name__)

_DEFAULT_MAINTENANCE_MSG = "系统维护中，暂不接收新任务，已完成任务仍可阅读与下载"


class UserError(ValueError):
    """请求本身有问题（参数、文件、缺少 Key 等），对应 4xx。"""


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class ServiceBusy(RuntimeError):
    """服务器暂时无法接收新任务（维护模式、排队已满、磁盘空间不足），对应 503。"""


class Limits(BaseModel):
    max_upload_mb: int
    max_pages: int
    max_active_jobs: int = 1_000_000
    free_max_pages: int | None = None
    free_max_active_jobs: int = 7
    min_free_disk_mb: int = 0
    retention_hours: int = 0  # 0 = 不自动删除
    maintenance_mode: bool = False
    site_notice: str = ""
    paddle_public_enabled: bool = False
    using_site_paddle_token: bool = False


PARSER_NAMES = {"mineru-cloud": "MinerU", "paddleocr-vl": "PaddleOCR"}


def _check_maintenance(maintenance_mode: bool, site_notice: str = "") -> None:
    if maintenance_mode:
        raise ServiceBusy(site_notice.strip() or _DEFAULT_MAINTENANCE_MSG)


def _check_paddle_access(parser: str, secrets: Secrets, limits: Limits | None) -> None:
    if (
        parser == "paddleocr-vl"
        and limits is not None
        and limits.using_site_paddle_token
        and not limits.paddle_public_enabled
    ):
        if secrets.channel_mode == "free":
            raise UserError("本站暂未向公众开放 PaddleOCR 解析服务")
        raise UserError("本站暂未向公众开放 PaddleOCR 解析服务，请先在设置中填写 PaddleOCR Token")


def _require_secrets(secrets: Secrets, *, parser: str | None) -> None:
    """parser 为本次要用的解析服务；不需要解析（已解析过）时为 None。"""
    if not secrets.llm_api_key:
        raise UserError("请先在设置中填写翻译服务的 API Key")
    if parser and not secrets.token_for(parser):
        raise UserError(f"请先在设置中填写 {PARSER_NAMES.get(parser, parser)} Token")


def _validate_upload_options(options: JobOptions, secrets: Secrets, limits: Limits | None = None) -> None:
    if not options.cloud_consent:  # 两个解析服务都在云端
        raise UserError(f"使用云端解析前需要确认文件会上传到 {PARSER_NAMES.get(options.parser, options.parser)}")
    _check_paddle_access(options.parser, secrets, limits)
    _require_secrets(secrets, parser=options.parser)


def _check_admission(
    pipeline: Pipeline,
    limits: Limits,
    channel_mode: Literal["byok", "free"] = "byok",
    *,
    already_reserved: bool = False,
) -> None:
    _check_maintenance(limits.maintenance_mode, limits.site_notice)
    offset = 1 if already_reserved else 0
    if channel_mode == "free":
        if pipeline.active_count("free") + pipeline.receiving_for("free") - offset >= limits.free_max_active_jobs:
            raise ServiceBusy("免费体验通道正忙，排队的任务较多，请稍后再试或切换自备 API Key")
    elif pipeline.active_count("byok") + pipeline.receiving_for("byok") - offset >= limits.max_active_jobs:
        raise ServiceBusy("服务器正忙，排队的任务太多，请稍后再试")


def create_job(
    pipeline: Pipeline,
    data: bytes,
    filename: str,
    options: JobOptions,
    secrets: Secrets,
    limits: Limits,
    submission_context: SubmissionContext | None = None,
) -> Job:
    """兼容直接提交字节的调用方，调度仍由调用线程完成。"""
    _check_maintenance(limits.maintenance_mode, limits.site_notice)
    if len(data) > limits.max_upload_mb * 1024 * 1024:
        raise UserError(f"文件超过 {limits.max_upload_mb} MB")
    if not data.startswith(b"%PDF"):
        raise UserError("只支持 PDF 文件")
    _validate_upload_options(options, secrets, limits)
    _check_admission(pipeline, limits, secrets.channel_mode)
    _check_upload_space(pipeline, limits)
    job = _persist_job(pipeline, data, filename, options, secrets, limits, submission_context)
    _submit_received_job(pipeline, job, secrets)
    return job


async def receive_upload(
    pipeline: Pipeline,
    upload: BinaryIO,
    filename: str,
    options: JobOptions,
    secrets: Secrets,
    limits: Limits,
    submission_context: SubmissionContext | None = None,
    *,
    already_reserved: bool = False,
) -> Job:
    _check_maintenance(limits.maintenance_mode, limits.site_notice)
    _validate_upload_options(options, secrets, limits)
    _check_admission(pipeline, limits, secrets.channel_mode, already_reserved=already_reserved)
    cancelled = threading.Event()

    def stage_and_persist() -> Job:
        tmp: Path | None = None
        limit = limits.max_upload_mb * 1024 * 1024
        try:
            root = _check_upload_space(pipeline, limits)
            prefix = bytearray()
            while len(prefix) < 4:
                if cancelled.is_set():
                    raise asyncio.CancelledError
                chunk = upload.read(min(4 - len(prefix), limit - len(prefix) + 1))
                if not chunk:
                    raise UserError("只支持 PDF 文件")
                prefix.extend(chunk)
                if len(prefix) > limit:
                    raise UserError(f"文件超过 {limits.max_upload_mb} MB")
            if prefix != b"%PDF":
                raise UserError("只支持 PDF 文件")
            if cancelled.is_set():
                raise asyncio.CancelledError

            with tempfile.NamedTemporaryFile(dir=root, prefix=".upload-", suffix=".part", delete=False) as f:
                tmp = Path(f.name)
                size = len(prefix)
                f.write(prefix)
                while not cancelled.is_set():
                    chunk = upload.read(min(256 * 1024, limit - size + 1))
                    if not chunk or cancelled.is_set():
                        break
                    size += len(chunk)
                    if size > limit:
                        raise UserError(f"文件超过 {limits.max_upload_mb} MB")
                    f.write(chunk)
            if cancelled.is_set():
                raise asyncio.CancelledError
            return _persist_job(pipeline, tmp, filename, options, secrets, limits, submission_context)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    async def accept() -> Job:
        job = await asyncio.to_thread(stage_and_persist)
        # 接收事务已提交，即使请求中断也要调度，避免留下孤立的 queued 任务。
        _submit_received_job(pipeline, job, secrets)
        return job

    if already_reserved:
        return await finish_on_cancel(asyncio.create_task(accept()), cancelled.set)
    if secrets.channel_mode == "free":
        pipeline.receiving_free += 1
    else:
        pipeline.receiving_byok += 1
    try:
        return await finish_on_cancel(asyncio.create_task(accept()), cancelled.set)
    finally:
        if secrets.channel_mode == "free":
            pipeline.receiving_free -= 1
        else:
            pipeline.receiving_byok -= 1


def _check_upload_space(pipeline: Pipeline, limits: Limits) -> Path:
    root = pipeline.deps.artifacts.root
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < limits.min_free_disk_mb * 1024 * 1024:
        raise ServiceBusy("服务器存储空间不足，请稍后再试")
    return root


def _persist_job(
    pipeline: Pipeline,
    data: bytes | Path,
    filename: str,
    options: JobOptions,
    secrets: Secrets,
    limits: Limits,
    submission_context: SubmissionContext | None,
) -> Job:
    try:
        pages = page_count(data)
    except ValueError as e:
        raise UserError(str(e)) from e
    max_pages = (
        min(limits.max_pages, limits.free_max_pages)
        if secrets.channel_mode == "free" and limits.free_max_pages is not None
        else limits.max_pages
    )
    if pages > max_pages:
        label = "免费体验上限" if secrets.channel_mode == "free" and limits.free_max_pages is not None else "上限"
        raise UserError(f"PDF 共 {pages} 页，超过{label} {max_pages} 页")
    if options.pages is not None:  # 只翻部分页（0 起页码）：版面仍解析全文，翻译阶段按页筛选
        if not options.pages or any(not 0 <= p < pages for p in options.pages):
            raise UserError(f"页码范围不对：这份 PDF 共 {pages} 页")
        options = options.model_copy(update={"pages": sorted(set(options.pages))})

    deps = pipeline.deps
    expires = datetime.now(UTC) + timedelta(hours=limits.retention_hours) if limits.retention_hours > 0 else None
    job = Job(
        id=new_job_id(),
        filename=Path(filename).name[:200] or "document.pdf",
        page_count=pages,
        options=options,
        model=secrets.llm_model,
        channel_mode=secrets.channel_mode,
        channel_provider_id=secrets.channel_provider_id,
        expires_at=expires,
    )
    try:
        original = deps.artifacts.path(job.id, "original.pdf")
        if isinstance(data, Path):
            data.replace(original)
        else:
            tmp = original.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(original)
        deps.store.create_job(job, submission_context)
    except Exception:
        # 数据库没有接收成功时不能留下不可追踪的临时正文。
        try:
            deps.artifacts.remove(job.id)
        except (OSError, ValueError):
            deps.store.queue_artifact_cleanup(job.id)
            log.warning("未接收任务的临时目录已加入清理重试队列")
        raise
    return job


def _submit_received_job(pipeline: Pipeline, job: Job, secrets: Secrets) -> None:
    deps = pipeline.deps
    try:
        pipeline.submit(job, secrets)
    except Exception:
        # 接收事务已经提交，调度失败也应返回原任务，供用户重试。
        log.exception("任务 %s 接收后调度失败", job.id)
        deps.secrets.drop(job.id)
        job.status, job.error = JobStatus.FAILED, "任务调度失败，请点击重试"
        deps.store.save_job(job)


def get_job(pipeline: Pipeline, job_id: str) -> Job:
    job = pipeline.deps.store.get_job(job_id)
    if job is None:
        raise NotFound("任务不存在")
    return job


def delete_job(pipeline: Pipeline, job_id: str) -> None:
    """删除任务及其全部文件。进行中的任务要等结束后才能删（后台线程可能还在写文件）。"""
    with pipeline.deletion_access(job_id) as allowed:
        if not allowed:
            raise UserError("任务进行中，结束后才能删除")
        get_job(pipeline, job_id)
        pipeline.deps.store.delete_job(job_id)
        _release_layout_cache(pipeline, job_id)
        _cleanup_artifacts_locked(pipeline, job_id)


def _release_layout_cache(pipeline: Pipeline, job_id: str) -> None:
    cache = pipeline.deps.layout_cache
    if cache is not None:
        try:
            cache.release_job(job_id)
        except Exception:
            log.warning("任务 %s 的版面缓存引用清理失败，将在下轮重试", job_id)


def _cleanup_artifacts_locked(pipeline: Pipeline, job_id: str) -> bool:
    if pipeline.deps.store.get_job(job_id) is not None:
        return False
    try:
        pipeline.deps.artifacts.remove(job_id)
    except (OSError, ValueError):
        log.warning("任务 %s 的目录清理失败，将在下轮重试", job_id)
        return False
    pipeline.deps.store.complete_artifact_cleanup(job_id)
    return True


def purge_expired(pipeline: Pipeline, retention_hours: int, now: datetime | None = None) -> int:
    """删除已到期的任务（跳过进行中的）与超过保留期的译文缓存，返回删除的任务数。

    没有到期时间的旧任务（保留期改为 24 小时之前创建的）不立即删除：从现在起再给一个保留期，留出下载时间。
    """
    now = now or datetime.now(UTC)
    removed = 0
    store = pipeline.deps.store
    for job in store.all_jobs():
        if retention_hours > 0 and job.expires_at is not None and job.expires_at <= now:
            with pipeline.deletion_access(job.id) as allowed:
                if allowed:
                    store.delete_job(job.id)
                    _release_layout_cache(pipeline, job.id)
                    removed += 1
            continue
        with pipeline.version_cleanup_access(job.id) as allowed:
            if not allowed or (current := store.get_job(job.id)) is None:
                continue
            if retention_hours > 0 and current.expires_at is None:
                current.expires_at = now + timedelta(hours=retention_hours)
                store.save_job(current)
            with suppress(OSError):
                purge_versions(pipeline.deps.artifacts, current, now)
    known: set[str] = set()
    if retention_hours > 0:
        known = {job.id for job in store.all_jobs()}
        cutoff = now - timedelta(hours=retention_hours)
        for job_id in pipeline.deps.artifacts.orphaned_jobs(known, cutoff):
            # 扫描和入队之间再次检查，保护同时接收或仍在写文件的任务。
            with pipeline.deletion_access(job_id) as allowed:
                if allowed and store.get_job(job_id) is None:
                    store.queue_artifact_cleanup(job_id)
    cleaned = 0
    for job_id in store.pending_artifact_cleanup():
        with pipeline.deletion_access(job_id) as allowed:
            if allowed and _cleanup_artifacts_locked(pipeline, job_id):
                cleaned += 1
    pipeline.deps.artifacts.purge_parser_tmp(pipeline.active_job_ids(), max_age_s=600.0)
    cached = pipeline.deps.cache.purge_older_than(retention_hours) if retention_hours > 0 else 0
    layout_cache = pipeline.deps.layout_cache
    if layout_cache is not None:
        try:
            if retention_hours == 0 or pipeline.deps.layout_cache_ttl_hours == 0:
                layout_cache.clear_all()
            else:
                layout_cache.purge_expired(known_job_ids=known, now_ts=int(now.timestamp()))
        except Exception:
            log.warning("版面缓存周期清理失败，将在下轮重试")
    if removed or cleaned or cached:
        store.checkpoint_after_purge()
    if removed or cached:
        log.info("已清理过期任务 %d 个、译文缓存 %d 条", removed, cached)
    return removed


# 导出 PDF 的生成方式（排版、字体、链接等）变了就加一，让已缓存的导出文件失效
EXPORT_CACHE_VERSION = 17  # 裁图水平邻字边界与中文交叉引用热点
_EXPORT_LOCKS = tuple(threading.Lock() for _ in range(16))


@dataclass(frozen=True)
class _Export:
    job: Job
    mode: ExportMode
    side: Side
    segments: list[Segment]
    path: Path
    digest: str


def _export_path(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side, digest: str) -> Path:
    return pipeline.deps.artifacts.job_dir(job_id) / "exports" / f"{mode}-{side}-{digest}.pdf"


def _export_snapshot(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side) -> _Export:
    with pipeline.content_access():
        job = get_job(pipeline, job_id)
        if job.status is not JobStatus.DONE:
            raise UserError("任务完成后才能下载")
        segments = pipeline.deps.store.segments(job_id)
    state = json.dumps(
        [EXPORT_CACHE_VERSION, job.content_version, [(s.id, s.target, s.status.value) for s in segments]],
        ensure_ascii=False,
    )
    digest = hashlib.sha256(state.encode()).hexdigest()[:16]
    return _Export(job, mode, side, segments, _export_path(pipeline, job_id, mode, side, digest), digest)


def _export_result(
    doc: Document, job: Job, mode: ExportMode, path: Path, digest: str, meta: "_ExportMeta"
) -> ExportResult:
    title = paper_title(doc.pages, fallback=doc.meta.title)
    filename = download_filename(title, job.filename, bilingual=mode == "dual")
    return ExportResult(filename, path, meta.size_bytes, digest, meta.overflow_pages, meta.crc32)


@dataclass(frozen=True)
class _ExportMeta:
    overflow_pages: tuple[int, ...]
    size_bytes: int
    crc32: int | None


def _read_export_meta(pdf: Path) -> _ExportMeta | None:
    try:
        actual_size = pdf.stat().st_size
        meta = json.loads(pdf.with_suffix(".meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or type(meta.get("v")) is not int or meta["v"] != EXPORT_CACHE_VERSION:
            return None
        pages = meta.get("overflow_pages")
        if not isinstance(pages, list):
            return None
        previous = 0
        for page in pages:
            if type(page) is not int or page <= previous:
                return None
            previous = page
        size = meta.get("size_bytes", actual_size)
        crc = meta.get("crc32")
        if (
            type(size) is not int
            or size != actual_size
            or (crc is not None and (type(crc) is not int or not 0 <= crc <= 0xFFFFFFFF))
        ):
            return None
        return _ExportMeta(tuple(pages), size, crc)
    except (OSError, ValueError, RecursionError):
        return None


def export_crc32(file: BinaryIO) -> int:
    crc = 0
    while chunk := file.read(64 * 1024):
        crc = zlib.crc32(chunk, crc)
    file.seek(0)
    return crc


def write_export_meta(pdf: Path, overflow_pages: tuple[int, ...], size_bytes: int, crc32: int) -> _ExportMeta:
    meta = pdf.with_suffix(".meta.json")
    with tempfile.NamedTemporaryFile(dir=pdf.parent, prefix=f".{pdf.stem}-", suffix=".part", delete=False) as file:
        tmp = Path(file.name)
    try:
        tmp.write_text(
            json.dumps(
                {"v": EXPORT_CACHE_VERSION, "overflow_pages": overflow_pages, "size_bytes": size_bytes, "crc32": crc32}
            ),
            encoding="utf-8",
        )
        tmp.replace(meta)
    finally:
        tmp.unlink(missing_ok=True)
    return _ExportMeta(overflow_pages, size_bytes, crc32)


def _write_export_meta(pdf: Path, overflow_pages: list[int]) -> _ExportMeta:
    with pdf.open("rb") as file:
        return write_export_meta(pdf, tuple(overflow_pages), pdf.stat().st_size, export_crc32(file))


def _cleanup_exports(export: _Export) -> None:
    out = export.path
    cutoff = time.time() - 900
    for old in out.parent.glob(f"{export.mode}-{export.side}-*.pdf"):
        with suppress(OSError):
            if old != out and old.stat().st_mtime < cutoff:
                meta = old.with_suffix(".meta.json")
                # 提交标记刚发布时，仍需留出完整的下载宽限期。
                if not meta.exists() or meta.stat().st_mtime < cutoff:
                    meta.unlink(missing_ok=True)
                    old.unlink()
    for meta in out.parent.glob("*.meta.json"):
        with suppress(OSError):
            pdf = meta.with_name(meta.name.removesuffix(".meta.json") + ".pdf")
            if not pdf.is_file() and meta.stat().st_mtime < cutoff:
                meta.unlink()
    for partial in out.parent.glob("*.part"):
        with suppress(OSError):
            if partial.stat().st_mtime < cutoff:
                partial.unlink()


def _render_export(pipeline: Pipeline, export: _Export) -> ExportResult:
    job_id, out = export.job.id, export.path
    deps = pipeline.deps
    with _EXPORT_LOCKS[hash(out) % len(_EXPORT_LOCKS)]:
        # 等待本机名额或路径锁期间，任务可能已重新进入翻译。
        if get_job(pipeline, job_id).status is not JobStatus.DONE:
            raise UserError("任务完成后才能下载")
        version = export.job.content_version
        doc = load_document(deps, job_id, version)
        pages = _read_export_meta(out)
        if pages is None:
            # PDF 缺失时，旧旁路文件也可能仍有效，必须在重建前撤下提交标记。
            out.with_suffix(".meta.json").unlink(missing_ok=True)
            try:
                overflow_pages = export_pdf_to_path(
                    deps.artifacts.path(job_id, "original.pdf"),
                    version_artifact(deps.artifacts, job_id, version, "background.pdf"),
                    doc, export.segments, export.mode, deps.font_dir, export.side,
                    _raw_links(pipeline, job_id, doc, export.segments, version), out=out,
                )  # fmt: skip
                pages = _write_export_meta(out, overflow_pages)
            finally:
                release_memory()
            with suppress(UserError, NotFound, OSError):
                if _export_snapshot(pipeline, job_id, export.mode, export.side).path == out:
                    with pipeline.export_cleanup_access(job_id) as allowed:
                        if allowed:
                            _cleanup_exports(export)
    return _export_result(doc, export.job, export.mode, out, export.digest, pages)


def _lookup_export(
    pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side
) -> tuple[_Export, ExportResult | None]:
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        export = _export_snapshot(pipeline, job_id, mode, side)
        pages = _read_export_meta(export.path)
        if pages is None:
            return export, None
        doc = load_document(pipeline.deps, job_id, export.job.content_version)
        return export, _export_result(doc, export.job, mode, export.path, export.digest, pages)


def published_export(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side, digest: str) -> ExportResult:
    """在调用方持有下载保护期间读取已提交的快照。"""
    job = get_job(pipeline, job_id)
    path = _export_path(pipeline, job_id, mode, side, digest)
    pages = _read_export_meta(path)
    if pages is None:
        raise NotFound("导出快照已失效，请重新下载")
    return _export_result(load_document(pipeline.deps, job_id, job.content_version), job, mode, path, digest, pages)


def export_job(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side = "right") -> tuple[str, Path]:
    """兼容同步调用，按格式和译文快照复用磁盘缓存。"""
    export, cached = _lookup_export(pipeline, job_id, mode, side)
    if cached is not None:
        return cached.filename, cached.path
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        result = _render_export(pipeline, export)
        return result.filename, result.path


async def prepare_export(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side = "right") -> ExportResult:
    export, cached = await run_thread(_lookup_export, pipeline, job_id, mode, side)
    if cached is not None:
        return cached

    return await pipeline.export_once(export.path, lambda: _render_export_async(pipeline, export))


async def _render_export_async(pipeline: Pipeline, export: _Export) -> ExportResult:
    with pipeline.export_access(export.job.id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        async with pipeline.local_slot():
            return await run_thread(_render_export, pipeline, export)


async def start_export(
    pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side
) -> tuple[str, ExportResult | None]:
    export, cached = await run_thread(_lookup_export, pipeline, job_id, mode, side)
    if cached is None:
        pipeline.start_export(export.path, lambda: _render_export_async(pipeline, export))
    return export.digest, cached


async def export_status(
    pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side, digest: str
) -> tuple[Literal["preparing", "ready", "failed", "expired"], ExportResult | None, str]:
    def lookup() -> ExportResult | None:
        with pipeline.export_access(job_id) as allowed:
            if not allowed:
                raise NotFound("任务不存在")
            get_job(pipeline, job_id)
            try:
                return published_export(pipeline, job_id, mode, side, digest)
            except NotFound:
                return None

    path = _export_path(pipeline, job_id, mode, side, digest)
    state, detail = pipeline.export_task_state(path)
    cached = await run_thread(lookup)
    if cached is not None:
        return "ready", cached, ""
    # 在读取磁盘期间可能刚发布，保留在途状态，下一次轮询即可命中。
    if state == "preparing":
        return state, None, detail
    state, detail = pipeline.export_task_state(path)
    return state, None, detail


def cancel_job(pipeline: Pipeline, job_id: str) -> Job:
    """取消排队中或进行中的任务。"""
    job = get_job(pipeline, job_id)
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING) or not pipeline.cancel(job_id):
        raise UserError("任务不在进行中，无法取消")
    return job


def retry_job(
    pipeline: Pipeline,
    job_id: str,
    secrets: Secrets,
    *,
    limits: Limits | None = None,
    maintenance_mode: bool = False,
    site_notice: str = "",
) -> Job:
    _check_maintenance(
        limits.maintenance_mode if limits is not None else maintenance_mode,
        limits.site_notice if limits is not None else site_notice,
    )
    if not pipeline.begin_mutation(job_id):
        raise UserError(pipeline.mutation_blocked_message(job_id))
    try:
        job = get_job(pipeline, job_id)
        if job.status is JobStatus.DONE and not pipeline.deps.store.count_segments(job_id, SegmentStatus.FAILED):
            raise UserError("任务已完成")
        needs_parser = job.stage == "parse" and not pipeline.deps.artifacts.path(job_id, "layout.json").exists()
        if needs_parser:
            _check_paddle_access(job.options.parser, secrets, limits)
        _require_secrets(secrets, parser=job.options.parser if needs_parser else None)
        if limits is not None:
            _check_admission(pipeline, limits, job.channel_mode)
        if bad_source(pipeline.deps.store.segments(job_id)):
            raise UserError(BAD_SOURCE_MESSAGE)
        if job.stage == "done":
            job.stage = "translate"  # 已完成但有失败段落：只重跑翻译和背景页
        if secrets.channel_mode == "free":
            job.channel_provider_id = secrets.channel_provider_id
            job.model = secrets.llm_model
        job.status, job.error = JobStatus.QUEUED, ""
        job.parse_queue_started_at = None
        pipeline.deps.store.save_job(job)
        pipeline.submit(job, secrets)
        return job
    finally:
        pipeline.end_mutation(job_id)


class DocumentView(BaseModel):
    job: Job
    document: Document
    segments: list[Segment]
    links: list[PdfLink] = []
    outline: list[OutlineItem] = []
    active_expansion: ExpansionOp | None = None


_LETTERS = re.compile(r"[^0-9a-z]+")


def _norm(text: str) -> str:
    return _LETTERS.sub("", text.lower())


def build_outline(pdf: Path, doc: Document, segments: list[Segment]) -> list[OutlineItem]:
    """目录以原文为准：优先用 PDF 书签；没有书签时用识别出的标题，按编号（“3.1” → 二级）定层级。
    每项尽量配上对应标题段落的译文。"""
    first = {s.block_ids[0]: s for s in segments}
    paper_blocks = {b.id for b in paper_title_blocks(doc.pages)}
    heads = []
    for block in doc.blocks():
        seg = first.get(block.id)
        text = " ".join(s.text for s in block.spans if s.kind == "text").strip()
        if (
            block.type is BlockType.TITLE
            and block.id not in paper_blocks
            and text
            and _norm(text) != _norm(doc.meta.title)
        ):
            heads.append((block, seg, text))

    def translated(seg: Segment | None) -> str:
        if seg is None or not seg.target or seg.status is SegmentStatus.FAILED:
            return ""
        return re.sub(r"<m\d+/>", "…", seg.target).strip()

    items = pdf_outline(pdf)
    if items:
        for item in items:  # 书签标题常不带编号：在同页附近的标题里找文字包含它的
            key = _norm(item.title)
            match = next((h for h in heads if key and key in _norm(h[2]) and abs(h[0].page - item.page) <= 1), None)
            if match is None:  # 文字对不上（如书签是缩写）：同一页离书签位置最近的标题，上下 60 点内
                near = [h for h in heads if h[0].page == item.page and abs(h[0].bbox.y0 - item.top) <= 60]
                match = min(near, key=lambda h: abs(h[0].bbox.y0 - item.top), default=None)
            if match:
                item.translated, item.segment_id = translated(match[1]), match[1].id if match[1] else None
        return items
    out = []
    roman_chapter = False
    previous_letter = ""
    for block, seg, text in heads:
        plain = RICH_TAG.sub("", text).lstrip()
        numbered = NUMBERED_HEADING.match(plain)
        letter = LETTER_HEADING.match(plain)
        sequential_letter = bool(previous_letter and letter and ord(plain[0]) == ord(previous_letter) + 1)
        if ROMAN_HEADING.match(plain) and not (roman_chapter and letter and (sequential_letter or block.level == 2)):
            roman_chapter = True
            previous_letter = ""
            inferred = 1
        elif roman_chapter and letter:
            previous_letter = plain[0]
            inferred = 2
        else:
            inferred = 1 + numbered[2].count(".") if numbered else 1
            if inferred == 1:
                roman_chapter = False
                previous_letter = ""
        level = block.level if block.level is not None else inferred
        out.append(OutlineItem(title=text, level=max(1, min(int(level), 4)), page=block.page, top=block.bbox.y0,
                               translated=translated(seg), segment_id=seg.id if seg else None))  # fmt: skip
    return out


def _reader_text(seg: Segment) -> str:
    """阅读器里一段显示的文字：没有译文或没通过校验时显示原文（与前端 entities/document 一致）。"""
    return seg.target if seg.target and seg.status is not SegmentStatus.FAILED else seg.source


# 单进程内使用固定数量的锁，避免任务不断增加时锁表也长期增长。
_LINK_CACHE_LOCKS = tuple(threading.Lock() for _ in range(32))


def _read_link_cache(cache: Path) -> list[PdfLink] | None:
    try:
        return [PdfLink.model_validate(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
    except (FileNotFoundError, ValueError, TypeError):
        # 旧版直接写最终文件，超时重试重叠时可能留下未写完的 JSON；重新提取即可修复。
        return None


def _raw_links(
    pipeline: Pipeline, job_id: str, doc: Document, segments: list[Segment], version: int = 1
) -> list[PdfLink]:
    """原文链接按任务缓存；首次并发阅读和下载共享一次提取，并原子发布缓存。"""
    cache = version_artifact(pipeline.deps.artifacts, job_id, version, f"links.v{LINKS_VERSION}.json")
    if (cached := _read_link_cache(cache)) is not None:
        return cached
    with _LINK_CACHE_LOCKS[hash(str(cache)) % len(_LINK_CACHE_LOCKS)]:
        if (cached := _read_link_cache(cache)) is not None:
            return cached
        started = time.monotonic()
        log.info("开始提取阅读链接 job=%s", job_id)
        links = page_links(pipeline.deps.artifacts.path(job_id, "original.pdf"), doc, segments)
        tmp = cache.with_suffix(".part")
        try:
            tmp.write_text(json.dumps([x.model_dump(mode="json") for x in links], ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("阅读链接已缓存 job=%s links=%d elapsed_ms=%d", job_id, len(links), _ms(started))
        return links


def document_view(pipeline: Pipeline, job_id: str) -> DocumentView:
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        with pipeline.content_access():
            job = get_job(pipeline, job_id)
            if STAGES.index("segment") > (STAGES.index(job.stage) if job.stage in STAGES else len(STAGES)):
                raise UserError("文档尚未解析完成")
            segments = pipeline.deps.store.segments(job_id)
            active = pipeline.active_expansion(job_id)
        doc = load_document(pipeline.deps, job_id, job.content_version)
        links: list[PdfLink] = []
        if job.status is JobStatus.DONE:
            raw = _raw_links(pipeline, job_id, doc, segments, job.content_version)
            links = place_links(raw, doc, segments, _reader_text)
        outline = build_outline(pipeline.deps.artifacts.path(job_id, "original.pdf"), doc, segments)
        return DocumentView(
            job=job, document=doc, segments=segments, links=links, outline=outline, active_expansion=active
        )


def _segment(pipeline: Pipeline, segment_id: str) -> Segment:
    seg = pipeline.deps.store.segment(segment_id)
    if seg is None:
        raise NotFound("段落不存在")
    return seg


def _record(
    pipeline: Pipeline, before: Segment, after: Segment, action: RevisionAction, reverts: int | None = None
) -> None:
    pipeline.deps.store.add_revision(_revision(before, after, action, reverts))


def _revision(before: Segment, after: Segment, action: RevisionAction, reverts: int | None = None) -> Revision:
    return Revision(
        job_id=after.job_id, segment_id=after.id, action=action, reverts=reverts,
        before=before.target, before_status=before.status, before_quality=before.quality,
        after=after.target, after_status=after.status, after_quality=after.quality,
    )  # fmt: skip


def edit_segment(
    pipeline: Pipeline,
    segment_id: str,
    target: str,
    formulas: Mapping[str, str] | None = None,
) -> Segment:
    cleaned = target.strip()
    if not cleaned:
        raise UserError("译文不能为空")
    seg = _segment(pipeline, segment_id)
    job = get_job(pipeline, seg.job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能编辑译文")
    if not pipeline.begin_mutation(seg.job_id):
        raise UserError(pipeline.mutation_blocked_message(seg.job_id))
    try:
        seg = _segment(pipeline, segment_id)
        job = get_job(pipeline, seg.job_id)
        if job.status is not JobStatus.DONE:
            raise UserError("任务完成后才能编辑译文")
        # 公式占位符少了或多了，阅读器与导出 PDF 里对应的公式会悄悄消失或重复，不保存
        if problems := placeholder_problems(seg.source, cleaned):
            raise UserError("译文里的公式不对：" + "；".join(problems) + "。每个公式占位符要各保留一次")
        if problems := rich_text_problems(cleaned):
            raise UserError("译文里的格式标记不对：" + "；".join(problems))
        updated_formulas: dict[str, str] | None = None
        if formulas:
            unknown_keys = [k for k in formulas if k not in seg.formulas]
            if unknown_keys:
                raise UserError(f"公式 {', '.join(unknown_keys)} 在本段中不存在")
            updated_formulas = dict(seg.formulas)
            for k, v in formulas.items():
                latex = v.strip()
                if not latex:
                    raise UserError(f"公式 {k} 不能为空")
                updated_formulas[k] = latex
        before = seg.model_copy(deep=True)
        seg.target, seg.status = cleaned, SegmentStatus.EDITED
        if updated_formulas is not None:
            seg.formulas = updated_formulas
        seg.failure_reason = ""
        pipeline.deps.store.save_segments([seg])
        if before.target != seg.target:
            _record(pipeline, before, seg, "edit")
        return seg
    finally:
        pipeline.end_mutation(seg.job_id)


GLOSSARY_MAX_BYTES = 2 * 1024 * 1024


async def parse_glossary(pipeline: Pipeline, secrets: Secrets, data: bytes, *, use_ai: bool) -> ParsedGlossary:
    """识别用户上传的术语表（不保存，结果交给用户确认）。use_ai 时用翻译服务读取任意格式，否则按规则识别。"""
    if len(data) > GLOSSARY_MAX_BYTES:
        raise UserError("术语表文件太大（上限 2 MB），可以拆成几个文件分别导入")
    if use_ai and not secrets.llm_api_key:
        raise UserError("AI 识别要用翻译服务，请先在设置中填写 API Key")
    if not use_ai:
        return await parse_upload(data, chat=None)
    async with chat_session(pipeline.deps.chat_factory(secrets)) as chat:
        return await parse_upload(data, chat=chat)


class LlmCheck(BaseModel):
    ok: bool
    model: str
    latency_ms: int
    reply: str = ""
    detail: str = ""


async def llm_models(pipeline: Pipeline, secrets: Secrets) -> list[str]:
    """从翻译服务拉取可用模型；不支持查询的服务仍可手动填写模型名。"""
    _require_secrets(secrets, parser=None)
    async with chat_session(pipeline.deps.chat_factory(secrets)) as chat:
        return await chat.list_models()


async def llm_check(pipeline: Pipeline, secrets: Secrets) -> LlmCheck:
    """发一个很短的请求，检查接口地址、Key、模型能否连通（费用可忽略）。"""
    started = time.monotonic()
    try:
        _require_secrets(secrets, parser=None)
        async with chat_session(pipeline.deps.chat_factory(secrets)) as chat:
            r = await chat.complete("只回复 OK 两个字母。", "ping", ChatOptions(max_tokens=16))
    except (ChatError, UserError) as e:
        return LlmCheck(ok=False, model=secrets.llm_model, latency_ms=_ms(started), detail=str(e))
    return LlmCheck(ok=True, model=secrets.llm_model, latency_ms=_ms(started), reply=r.text.strip()[:40])


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def revisions(pipeline: Pipeline, job_id: str) -> list[Revision]:
    get_job(pipeline, job_id)
    return pipeline.deps.store.revisions(job_id)


def revert_revision(pipeline: Pipeline, revision_id: int) -> Segment:
    """把这一段恢复到这一步之前的样子；这一段在这一步之后的修改一并作废（不同段落互不影响）。"""
    store = pipeline.deps.store
    rev = store.revision(revision_id)
    if rev is None:
        raise NotFound("修改记录不存在")
    if rev.action == "revert" or rev.reverted_by is not None:
        raise UserError("这一步已经撤回过了")
    if get_job(pipeline, rev.job_id).status is not JobStatus.DONE:
        raise UserError("任务完成后才能撤回修改")
    if not pipeline.begin_mutation(rev.job_id):
        raise UserError(pipeline.mutation_blocked_message(rev.job_id))
    try:
        seg = _segment(pipeline, rev.segment_id)
        before = seg.model_copy()
        seg.target, seg.status, seg.quality = rev.before, rev.before_status, rev.before_quality
        store.save_segments([seg])
        undo = store.add_revision(
            Revision(
                job_id=seg.job_id, segment_id=seg.id, action="revert", reverts=rev.id,
                before=before.target, before_status=before.status, before_quality=before.quality,
                after=seg.target, after_status=seg.status, after_quality=seg.quality,
            )
        )  # fmt: skip
        later = [
            r.model_copy(update={"reverted_by": undo.id})
            for r in store.revisions(seg.job_id)
            if r.segment_id == seg.id
            and r.id >= rev.id
            and r.id != undo.id
            and r.action != "revert"
            and r.reverted_by is None
        ]
        store.save_revisions(later)
        return seg
    finally:
        pipeline.end_mutation(rev.job_id)


async def retranslate_segment(
    pipeline: Pipeline,
    segment_id: str,
    quality: Quality,
    secrets: Secrets,
    user_terms: dict[str, str] | None = None,
    *,
    maintenance_mode: bool = False,
    site_notice: str = "",
    instruction: str = "",
) -> tuple[Segment, bool, str]:
    """user_terms：浏览器里最新的用户术语库（用户纠正术语后重翻这一段时用）；不传则用建任务时的。"""
    _check_maintenance(maintenance_mode, site_notice)
    seg = _segment(pipeline, segment_id)
    (out,) = await _retranslate_segments(
        pipeline,
        seg.job_id,
        [segment_id],
        quality,
        secrets,
        user_terms,
        instruction=instruction,
        maintenance_mode=maintenance_mode,
        site_notice=site_notice,
    )
    return out


MAX_RETRANSLATE = 20  # 单次最多 20 段；整次调用含排队、重试最多 45 秒，先于网关返回


async def retranslate_segments(
    pipeline: Pipeline,
    job_id: str,
    segment_ids: list[str],
    quality: Quality,
    secrets: Secrets,
    user_terms: dict[str, str] | None = None,
    *,
    maintenance_mode: bool = False,
    site_notice: str = "",
) -> list[Segment]:
    """一次重翻同一任务的多段（改了术语译法后重翻用到它的段落），与整篇翻译一样分批发给模型。
    每段照常记入修改记录、可以撤回，费用计入任务。"""
    results = await _retranslate_segments(
        pipeline,
        job_id,
        segment_ids,
        quality,
        secrets,
        user_terms,
        maintenance_mode=maintenance_mode,
        site_notice=site_notice,
    )
    return [seg for seg, _, _ in results]


async def _retranslate_segments(
    pipeline: Pipeline,
    job_id: str,
    segment_ids: list[str],
    quality: Quality,
    secrets: Secrets,
    user_terms: dict[str, str] | None,
    *,
    maintenance_mode: bool,
    site_notice: str,
    instruction: str = "",
) -> list[tuple[Segment, bool, str]]:
    _check_maintenance(maintenance_mode, site_notice)
    _require_secrets(secrets, parser=None)
    if not 1 <= len(segment_ids) <= MAX_RETRANSLATE:
        raise UserError(f"一次最多重翻 {MAX_RETRANSLATE} 段")
    job = get_job(pipeline, job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能重翻段落")
    if not pipeline.begin_mutation(job_id):
        raise UserError(pipeline.mutation_blocked_message(job_id))
    try:
        job = get_job(pipeline, job_id)
        if job.status is not JobStatus.DONE:
            raise UserError("任务完成后才能重翻段落")
        deps = pipeline.deps
        all_segs = deps.store.segments(job_id)
        by_id = {s.id: s for s in all_segs}
        befores: dict[str, Segment] = {}
        for sid in dict.fromkeys(segment_ids):
            before = by_id.get(sid)
            if before is None:
                _segment(pipeline, sid)
                raise NotFound("段落不属于这个任务")
            befores[sid] = before
        segs = list(befores.values())
        terms = user_terms if user_terms is not None else job.options.user_terms
        if bad_source(segs):
            raise UserError(BAD_SOURCE_MESSAGE)
        ctx, selection = await asyncio.to_thread(load_context, deps, job_id, terms)
        prices = deps.price_overrides_loader() if deps.price_overrides_loader else None
        total_budget_s = 45.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total_budget_s
        try:
            async with pipeline.translation_slot(job.channel_mode, timeout=total_budget_s):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ServiceBusy("翻译通道正忙，等待重翻超时，请稍后再试")
                async with chat_session(deps.chat_factory(secrets)) as chat:
                    outs, report = await translate(
                        [s.model_copy(update={"status": SegmentStatus.PENDING}) for s in segs],
                        chat=chat,
                        cache=deps.cache,
                        ctx=ctx,
                        lookup_term_hits=selection.match_segment,
                        context_segments=all_segs,
                        opts=TranslateOptions(
                            quality=quality,
                            concurrency=pipeline.llm_concurrency(secrets),
                            use_cache=False,
                            custom_instruction=instruction,
                            total_timeout_s=remaining,
                            price_overrides=prices,
                        ),
                    )
        except TimeoutError as exc:
            raise ServiceBusy("翻译通道正忙，等待重翻超时，请稍后再试") from exc
        results = []
        revisions_to_add = []
        for out in outs:
            before = befores[out.id]
            applied = out.status is SegmentStatus.DONE
            notice = ""
            if (
                out.status is SegmentStatus.FAILED
                and before.target
                and before.status in (SegmentStatus.DONE, SegmentStatus.EDITED)
            ):
                # 重翻未成功时保留上一份可用译文，不能把已翻好的段落退回原文。
                out.target, out.status, out.quality = before.target, before.status, before.quality
                notice = "本次重翻未通过校验，已保留原有译文"
            if applied:
                revisions_to_add.append(_revision(before, out, "careful" if quality == "careful" else "retranslate"))
            results.append((out, applied, notice))
        try:
            deps.store.commit_retranslate(
                expected_job=job,
                expected_before=befores,
                updated_segments=outs,
                revisions_to_add=revisions_to_add,
                cost_delta_cny=report.cost_cny,
                used_fallback=report.used_fallback,
            )
        except RetranslateConflict as exc:
            raise Conflict(str(exc)) from exc
        return results
    finally:
        pipeline.end_mutation(job_id)


class TermUse(BaseModel):
    """这篇论文用到的一个术语。"""

    source: str
    target: str
    origin: Literal["user", "paper", "library"]  # 我的术语库 / 本文自动抽取 / 公共词库
    segment_ids: list[str]  # 原文里出现它的段落


def job_terms(pipeline: Pipeline, job_id: str) -> list[TermUse]:
    """这篇论文用到了哪些术语、各在哪几段（出现得多的在前）。来源与译法按建任务时的用户术语库计算；
    译文有没有用上推荐译法由前端检查（要按浏览器里最新的术语库和当前译文算）。"""
    deps = pipeline.deps
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        with pipeline.content_access():
            job = get_job(pipeline, job_id)
            segments = deps.store.segments(job_id)
        doc = load_document(deps, job_id, job.content_version)
        _, selection = load_context(deps, job_id, job.options.user_terms, doc)
    origins: tuple[Literal["user", "paper", "library"], ...] = ("user", "paper", "library")
    uses: dict[str, TermUse] = {}
    for seg in segments:
        seen: set[str] = set()
        for origin, hits in zip(origins, selection.match_segment(seg.source), strict=True):
            for source, target in hits.items():
                key = normalize_term_key(source)
                if key in seen:
                    continue
                seen.add(key)
                use = uses.get(key)
                if use is None:
                    use = uses[key] = TermUse(source=source, target=target, origin=origin, segment_ids=[])
                elif origins.index(origin) < origins.index(use.origin):
                    use.source, use.target, use.origin = source, target, origin
                use.segment_ids.append(seg.id)
    return sorted(uses.values(), key=lambda u: (-len(u.segment_ids), u.source.lower()))


def formula_crop(pipeline: Pipeline, segment_id: str, key: str) -> bytes:
    seg = _segment(pipeline, segment_id)
    ref = seg.formula_refs.get(key)
    if ref is None:
        raise NotFound("公式不存在")
    return crop_png(pipeline.deps.artifacts.path(seg.job_id, "original.pdf"), ref.page, ref.bbox)


_COPYABLE_MEDIA_TYPES = frozenset({BlockType.FIGURE, BlockType.TABLE})


def block_crop(pipeline: Pipeline, job_id: str, block_id: str) -> bytes:
    """按 MinerU 解析出的图表/表格坐标从 original.pdf 高清裁剪白底 PNG。"""
    deps = pipeline.deps
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        with pipeline.content_access():
            job = get_job(pipeline, job_id)
        doc = load_document(deps, job_id, job.content_version)
        block = doc.block(block_id)
        if block is None or block.type not in _COPYABLE_MEDIA_TYPES:
            raise NotFound("图表不存在")
        pdf = deps.artifacts.path(job_id, "original.pdf")
        if not pdf.is_file():
            raise NotFound("原文件不存在")
        png = crop_block_png(pdf, block.page, block.bbox)
    if not png:
        raise NotFound("图表区域为空")
    return png


class Estimate(BaseModel):
    pages: int
    cny_low: float | None
    cny_high: float | None
    peak: bool
    minutes: float


# M3 实测：DeepSeek Flash 默认档每页约 ¥0.0023–0.0037（空闲），精翻约 4 倍；
# 耗时主要在 MinerU 排队（30 秒到 4 分钟）与翻译（每 100 段约 20 秒）
_PER_PAGE = {"default": (0.0023, 0.0040), "careful": (0.009, 0.016)}


def estimate(
    pages: int,
    quality: Quality,
    model: str,
    *,
    prices: Mapping[str, Price] | None = None,
) -> Estimate:
    peak = is_deepseek_peak()
    unit_usage = ChatUsage(completion_tokens=1_000_000)
    unit_cost = cost_cny(model, unit_usage, peak=peak, prices=prices)
    base_unit = cost_cny("deepseek-flash", unit_usage, peak=False)
    if unit_cost is not None and base_unit:
        factor = unit_cost / base_unit
        low, high = (round(pages * v * factor, 3) for v in _PER_PAGE[quality])
    else:
        low = high = None
    return Estimate(pages=pages, cny_low=low, cny_high=high, peak=peak, minutes=round(1 + pages * 0.15, 1))
