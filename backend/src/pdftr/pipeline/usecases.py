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
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel

from pdftr.domain.document_name import download_filename, paper_title, paper_title_blocks
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
from pdftr.domain.llm import ChatError, ChatOptions
from pdftr.domain.submissions import SubmissionContext
from pdftr.extraction.chars import page_count
from pdftr.extraction.outline import pdf_outline
from pdftr.glossary.matcher import TermMatcher, combine
from pdftr.glossary.user_import import ParsedGlossary, parse_upload
from pdftr.pipeline.memory import release_memory
from pdftr.pipeline.runner import ExportResult, Pipeline, chat_session, load_context, load_document
from pdftr.pipeline.secrets import Secrets
from pdftr.pipeline.workers import finish_on_cancel, run_thread
from pdftr.rendering.background import crop_png
from pdftr.rendering.export import ExportMode, Side, export_pdf_to_path, page_links, place_links
from pdftr.rendering.links import VERSION as LINKS_VERSION
from pdftr.rendering.links import PdfLink
from pdftr.translation.batching import placeholder_problems, rich_text_problems
from pdftr.translation.cost import PRICES, is_deepseek_peak
from pdftr.translation.service import TranslateOptions, translate

log = logging.getLogger(__name__)


class UserError(ValueError):
    """请求本身有问题（参数、文件、缺少 Key 等），对应 4xx。"""


class NotFound(LookupError):
    pass


class ServiceBusy(RuntimeError):
    """服务器暂时无法接收新任务（排队已满、磁盘空间不足），对应 503。"""


class Limits(BaseModel):
    max_upload_mb: int
    max_pages: int
    max_active_jobs: int = 1_000_000
    min_free_disk_mb: int = 0
    retention_hours: int = 0  # 0 = 不自动删除


PARSER_NAMES = {"mineru-cloud": "MinerU", "paddleocr-vl": "PaddleOCR"}


def _require_secrets(secrets: Secrets, *, parser: str | None) -> None:
    """parser 为本次要用的解析服务；不需要解析（已解析过）时为 None。"""
    if not secrets.llm_api_key:
        raise UserError("请先在设置中填写翻译服务的 API Key")
    if parser and not secrets.token_for(parser):
        raise UserError(f"请先在设置中填写 {PARSER_NAMES.get(parser, parser)} Token")


def _validate_upload_options(options: JobOptions, secrets: Secrets) -> None:
    if not options.cloud_consent:  # 两个解析服务都在云端
        raise UserError(f"使用云端解析前需要确认文件会上传到 {PARSER_NAMES.get(options.parser, options.parser)}")
    _require_secrets(secrets, parser=options.parser)


def _check_admission(pipeline: Pipeline, limits: Limits) -> None:
    if pipeline.active_count() + pipeline.receiving >= limits.max_active_jobs:
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
    if len(data) > limits.max_upload_mb * 1024 * 1024:
        raise UserError(f"文件超过 {limits.max_upload_mb} MB")
    if not data.startswith(b"%PDF"):
        raise UserError("只支持 PDF 文件")
    _validate_upload_options(options, secrets)
    _check_admission(pipeline, limits)
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
) -> Job:
    _validate_upload_options(options, secrets)
    _check_admission(pipeline, limits)
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

    pipeline.receiving += 1
    try:
        return await finish_on_cancel(asyncio.create_task(accept()), cancelled.set)
    finally:
        pipeline.receiving -= 1


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
    if pages > limits.max_pages:
        raise UserError(f"PDF 共 {pages} 页，超过上限 {limits.max_pages} 页")
    if options.pages is not None:  # 只翻部分页（0 起页码）：版面仍解析全文，翻译阶段按页筛选
        if not options.pages or any(not 0 <= p < pages for p in options.pages):
            raise UserError(f"页码范围不对：这份 PDF 共 {pages} 页")
        options = options.model_copy(update={"pages": sorted(set(options.pages))})

    deps = pipeline.deps
    expires = datetime.now(UTC) + timedelta(hours=limits.retention_hours) if limits.retention_hours > 0 else None
    job = Job(id=new_job_id(), filename=Path(filename).name[:200] or "document.pdf", page_count=pages,
              options=options, model=secrets.llm_model, expires_at=expires)  # fmt: skip
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
        _cleanup_artifacts_locked(pipeline, job_id)


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
    for job in store.all_jobs() if retention_hours > 0 else []:
        if job.expires_at is None:
            job.expires_at = now + timedelta(hours=retention_hours)
            pipeline.deps.store.save_job(job)
            continue
        if job.expires_at <= now:
            with pipeline.deletion_access(job.id) as allowed:
                if allowed:
                    store.delete_job(job.id)
                    removed += 1
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
    cached = pipeline.deps.cache.purge_older_than(retention_hours) if retention_hours > 0 else 0
    if removed or cleaned or cached:
        store.checkpoint_after_purge()
    if removed or cached:
        log.info("已清理过期任务 %d 个、译文缓存 %d 条", removed, cached)
    return removed


# 导出 PDF 的生成方式（排版、字体、链接等）变了就加一，让已缓存的导出文件失效
EXPORT_CACHE_VERSION = 3  # 整块粗体正文改用普通译文字体，旧导出文件不能继续命中


@dataclass(frozen=True)
class _Export:
    job: Job
    mode: ExportMode
    side: Side
    segments: list[Segment]
    path: Path


def _export_snapshot(pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side) -> _Export:
    job = get_job(pipeline, job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能下载")
    deps = pipeline.deps
    segments = deps.store.segments(job_id)
    state = json.dumps([EXPORT_CACHE_VERSION, [(s.id, s.target, s.status.value) for s in segments]], ensure_ascii=False)
    digest = hashlib.sha256(state.encode()).hexdigest()[:16]
    folder = deps.artifacts.job_dir(job_id) / "exports"
    return _Export(job, mode, side, segments, folder / f"{mode}-{side}-{digest}.pdf")


def _export_result(doc: Document, export: _Export) -> ExportResult:
    title = paper_title(doc.pages, fallback=doc.meta.title)
    filename = download_filename(title, export.job.filename, bilingual=export.mode == "dual")
    return ExportResult(filename, export.path, export.path.stat().st_size)


def _render_export(pipeline: Pipeline, export: _Export) -> ExportResult:
    job_id, out = export.job.id, export.path
    # 等待本机名额期间，任务可能已重新进入翻译。
    if get_job(pipeline, job_id).status is not JobStatus.DONE:
        raise UserError("任务完成后才能下载")
    deps = pipeline.deps
    doc = load_document(deps, job_id)
    if not out.exists():
        try:
            export_pdf_to_path(
                deps.artifacts.path(job_id, "original.pdf"), deps.artifacts.path(job_id, "background.pdf"),
                doc, export.segments, export.mode, deps.font_dir, export.side,
                _raw_links(pipeline, job_id, doc, export.segments), out=out,
            )  # fmt: skip
        finally:
            release_memory()
        with suppress(UserError, NotFound, OSError):
            if _export_snapshot(pipeline, job_id, export.mode, export.side).path == out:
                with pipeline.export_cleanup_access(job_id) as allowed:
                    if allowed:
                        cutoff = time.time() - 60
                        for old in out.parent.glob(f"{export.mode}-{export.side}-*.pdf"):
                            with suppress(OSError):
                                if old != out and old.stat().st_mtime < cutoff:
                                    old.unlink()
    return _export_result(doc, export)


def _lookup_export(
    pipeline: Pipeline, job_id: str, mode: ExportMode, side: Side
) -> tuple[_Export, ExportResult | None]:
    with pipeline.export_access(job_id) as allowed:
        if not allowed:
            raise NotFound("任务不存在")
        export = _export_snapshot(pipeline, job_id, mode, side)
        cached = _export_result(load_document(pipeline.deps, job_id), export) if export.path.exists() else None
        return export, cached


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

    async def render() -> ExportResult:
        with pipeline.export_access(job_id) as allowed:
            if not allowed:
                raise NotFound("任务不存在")
            async with pipeline.local_slot():
                return await run_thread(_render_export, pipeline, export)

    return await pipeline.export_once(export.path, render)


def cancel_job(pipeline: Pipeline, job_id: str) -> Job:
    """取消排队中或进行中的任务。"""
    job = get_job(pipeline, job_id)
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING) or not pipeline.cancel(job_id):
        raise UserError("任务不在进行中，无法取消")
    return job


def retry_job(pipeline: Pipeline, job_id: str, secrets: Secrets) -> Job:
    job = get_job(pipeline, job_id)
    if pipeline.is_running(job_id):
        raise UserError("任务正在进行中")
    if job.status is JobStatus.DONE and not pipeline.deps.store.count_segments(job_id, SegmentStatus.FAILED):
        raise UserError("任务已完成")
    _require_secrets(secrets, parser=job.options.parser if job.stage == "parse" else None)
    if not pipeline.begin_mutation(job_id):
        raise UserError("任务正在修改，请稍后再试")
    try:
        if job.stage == "done":
            job.stage = "translate"  # 已完成但有失败段落：只重跑翻译和背景页
        job.status, job.error = JobStatus.QUEUED, ""
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


_NUMBERED = re.compile(r"^\s*([A-Z]|\d+)((?:\.\d+)*)\.?\s+\S")  # “3.1 Encoder”“A.2 Proofs”
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
    for block, seg, text in heads:
        m = _NUMBERED.match(text)
        level = block.level if block.level is not None else (1 + m.group(2).count(".") if m else 1)
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


def _raw_links(pipeline: Pipeline, job_id: str, doc: Document, segments: list[Segment]) -> list[PdfLink]:
    """原文链接按任务缓存；首次并发阅读和下载共享一次提取，并原子发布缓存。"""
    cache = pipeline.deps.artifacts.path(job_id, f"links.v{LINKS_VERSION}.json")
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
    job = get_job(pipeline, job_id)
    if STAGES.index("segment") > (STAGES.index(job.stage) if job.stage in STAGES else len(STAGES)):
        raise UserError("文档尚未解析完成")
    doc, segments = load_document(pipeline.deps, job_id), pipeline.deps.store.segments(job_id)
    links: list[PdfLink] = []
    if job.status is JobStatus.DONE:
        links = place_links(_raw_links(pipeline, job_id, doc, segments), doc, segments, _reader_text)
    outline = build_outline(pipeline.deps.artifacts.path(job_id, "original.pdf"), doc, segments)
    return DocumentView(job=job, document=doc, segments=segments, links=links, outline=outline)


def _segment(pipeline: Pipeline, segment_id: str) -> Segment:
    seg = pipeline.deps.store.segment(segment_id)
    if seg is None:
        raise NotFound("段落不存在")
    return seg


def _record(
    pipeline: Pipeline, before: Segment, after: Segment, action: RevisionAction, reverts: int | None = None
) -> None:
    pipeline.deps.store.add_revision(
        Revision(
            job_id=after.job_id, segment_id=after.id, action=action, reverts=reverts,
            before=before.target, before_status=before.status, before_quality=before.quality,
            after=after.target, after_status=after.status, after_quality=after.quality,
        )
    )  # fmt: skip


def edit_segment(pipeline: Pipeline, segment_id: str, target: str) -> Segment:
    seg = _segment(pipeline, segment_id)
    job = get_job(pipeline, seg.job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能编辑译文")
    if not pipeline.begin_mutation(seg.job_id):
        raise UserError("任务正在修改，请稍后再试")
    try:
        seg = _segment(pipeline, segment_id)
        job = get_job(pipeline, seg.job_id)
        if job.status is not JobStatus.DONE:
            raise UserError("任务完成后才能编辑译文")
        # 公式占位符少了或多了，阅读器与导出 PDF 里对应的公式会悄悄消失或重复，不保存
        if problems := placeholder_problems(seg.source, target):
            raise UserError("译文里的公式不对：" + "；".join(problems) + "。每个公式占位符要各保留一次")
        if problems := rich_text_problems(target):
            raise UserError("译文里的格式标记不对：" + "；".join(problems))
        before = seg.model_copy()
        seg.target, seg.status = target.strip(), SegmentStatus.EDITED
        seg.failure_reason = ""
        pipeline.deps.store.save_segments([seg])
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
        raise UserError("任务正在修改，请稍后再试")
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
    pipeline: Pipeline, segment_id: str, quality: Quality, secrets: Secrets, user_terms: dict[str, str] | None = None
) -> Segment:
    """user_terms：浏览器里最新的用户术语库（用户纠正术语后重翻这一段时用）；不传则用建任务时的。"""
    seg = _segment(pipeline, segment_id)
    (out,) = await retranslate_segments(pipeline, seg.job_id, [segment_id], quality, secrets, user_terms)
    return out


MAX_RETRANSLATE = 20  # 单次最多 20 段；整次调用含排队、重试最多 45 秒，先于网关返回


async def retranslate_segments(
    pipeline: Pipeline,
    job_id: str,
    segment_ids: list[str],
    quality: Quality,
    secrets: Secrets,
    user_terms: dict[str, str] | None = None,
) -> list[Segment]:
    """一次重翻同一任务的多段（改了术语译法后重翻用到它的段落），与整篇翻译一样分批发给模型。
    每段照常记入修改记录、可以撤回，费用计入任务。"""
    _require_secrets(secrets, parser=None)
    if not 1 <= len(segment_ids) <= MAX_RETRANSLATE:
        raise UserError(f"一次最多重翻 {MAX_RETRANSLATE} 段")
    job = get_job(pipeline, job_id)
    if job.status is not JobStatus.DONE:
        raise UserError("任务完成后才能重翻段落")
    if not pipeline.begin_mutation(job_id):
        raise UserError("任务正在修改，请稍后再试")
    try:
        segs = [_segment(pipeline, sid) for sid in dict.fromkeys(segment_ids)]
        if any(s.job_id != job_id for s in segs):
            raise NotFound("段落不属于这个任务")
        terms = user_terms if user_terms is not None else job.options.user_terms
        befores = {s.id: s.model_copy() for s in segs}
        for s in segs:
            s.status = SegmentStatus.PENDING
        deps = pipeline.deps
        async with chat_session(deps.chat_factory(secrets)) as chat:
            outs, report = await translate(
                segs, chat=chat, cache=deps.cache, ctx=load_context(deps, job_id, terms),
                lookup_terms=combine(terms, deps.term_matcher()),
                opts=TranslateOptions(quality=quality, use_cache=False, total_timeout_s=45),
            )  # fmt: skip
        for out in outs:
            before = befores[out.id]
            if (
                out.status is SegmentStatus.FAILED
                and before.target
                and before.status in (SegmentStatus.DONE, SegmentStatus.EDITED)
            ):
                # 重翻未成功时保留上一份可用译文，不能把已翻好的段落退回原文。
                out.target, out.status, out.quality = before.target, before.status, before.quality
        deps.store.save_segments(outs)
        for out in outs:
            _record(pipeline, befores[out.id], out, "careful" if quality == "careful" else "retranslate")
        if report.cost_cny is not None:  # 重翻、精修的费用也计入任务花费
            job = get_job(pipeline, job_id)
            job.cost_cny = (job.cost_cny or 0) + report.cost_cny
            deps.store.save_job(job)
        return outs
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
    job = get_job(pipeline, job_id)
    deps = pipeline.deps
    user = job.options.user_terms or {}
    paper = load_context(deps, job_id, user).terms
    lookup_paper = TermMatcher(paper, min_length=2).match if paper else None
    lookup_rest = combine(user, deps.term_matcher())
    mine = {k.lower() for k in user}
    uses: dict[str, TermUse] = {}
    for seg in deps.store.segments(job_id):
        hits = (lookup_rest(seg.source) if lookup_rest else {}) | (lookup_paper(seg.source) if lookup_paper else {})
        for source, target in hits.items():
            k = source.lower()
            if k not in uses:
                origin = "user" if k in mine else "paper" if source in paper else "library"
                uses[k] = TermUse(source=source, target=target, origin=origin, segment_ids=[])
            uses[k].segment_ids.append(seg.id)
    return sorted(uses.values(), key=lambda u: (-len(u.segment_ids), u.source.lower()))


def formula_crop(pipeline: Pipeline, segment_id: str, key: str) -> bytes:
    seg = _segment(pipeline, segment_id)
    ref = seg.formula_refs.get(key)
    if ref is None:
        raise NotFound("公式不存在")
    return crop_png(pipeline.deps.artifacts.path(seg.job_id, "original.pdf"), ref.page, ref.bbox)


class Estimate(BaseModel):
    pages: int
    cny_low: float | None
    cny_high: float | None
    peak: bool
    minutes: float


# M3 实测：DeepSeek Flash 默认档每页约 ¥0.0023–0.0037（空闲），精翻约 4 倍；
# 耗时主要在 MinerU 排队（30 秒到 4 分钟）与翻译（每 100 段约 20 秒）
_PER_PAGE = {"default": (0.0023, 0.0040), "careful": (0.009, 0.016)}


def estimate(pages: int, quality: Quality, model: str) -> Estimate:
    peak = is_deepseek_peak()
    if model in PRICES:
        factor = PRICES[model].peak_factor if peak else 1.0
        if model != "deepseek-flash":  # 其他已知模型按输出单价折算
            factor *= PRICES[model].output / PRICES["deepseek-flash"].output
        low, high = (round(pages * v * factor, 3) for v in _PER_PAGE[quality])
    else:
        low = high = None
    return Estimate(pages=pages, cny_low=low, cny_high=high, peak=peak, minutes=round(1 + pages * 0.15, 1))
